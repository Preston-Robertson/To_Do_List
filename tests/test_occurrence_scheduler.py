"""Synthetic, isolated checks for history-preserving occurrence scheduling."""
from __future__ import annotations

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError

from luigi_web.modules.tasks import occurrences, repository as db


class SchedulerPolicyTests(unittest.TestCase):
    def test_default_external_never_acquires_engine(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch.object(db, "get_engine") as engine:
            self.assertEqual(db.reactivate_due_recurring(), 0)
            self.assertEqual(occurrences.scheduler_state()["owner"], "external")
            self.assertFalse(occurrences.scheduler_state()["enabled"])
            engine.assert_not_called()

    def test_explicit_external_never_acquires_engine(self) -> None:
        with patch.dict(os.environ, {"LUIGI_WEB_RECURRENCE_OWNER": "external"}), patch.object(db, "get_engine") as engine:
            self.assertEqual(db.reactivate_due_recurring(), 0)
            engine.assert_not_called()

    def test_invalid_owner_fails_closed_without_echoing_value(self) -> None:
        with patch.dict(os.environ, {"LUIGI_WEB_RECURRENCE_OWNER": "invalid-example"}), patch.object(db, "get_engine") as engine:
            with self.assertRaisesRegex(ValueError, "^Invalid recurrence scheduler owner$"):
                db.reactivate_due_recurring()
            engine.assert_not_called()

    def test_state_is_policy_only_and_names_deployment_requirement(self) -> None:
        with patch.dict(os.environ, {"LUIGI_WEB_RECURRENCE_OWNER": "web"}), patch.object(db, "get_engine") as engine:
            state = occurrences.scheduler_state()
            self.assertEqual(state["owner"], "web")
            self.assertTrue(state["enabled"])
            self.assertIn("legacy reset scheduler to be disabled", state["message"])
            engine.assert_not_called()


class OccurrenceSchedulerTests(unittest.TestCase):
    def make_engine(self, missing_fields=()):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        engine = create_engine(
            "sqlite+pysqlite:///" + (Path(temporary.name) / "synthetic.sqlite").as_posix(),
            connect_args={"timeout": 15},
        )
        self.addCleanup(engine.dispose)
        integer_fields = {
            "priority", "completed", "recurring", "recurring_interval",
            "recurring_month_ordinal", "recurring_month_weekday", "archived",
        }
        with engine.begin() as conn:
            for table in ("tasks", "recurring_tasks"):
                columns = ["id INTEGER PRIMARY KEY AUTOINCREMENT", "uuid TEXT UNIQUE NOT NULL"]
                for field in db._TASK_COLUMNS:
                    if field == "uuid" or (table == "recurring_tasks" and field in missing_fields):
                        continue
                    field_type = (
                        "INTEGER" if field in integer_fields else
                        "REAL" if field in {"estimated_time", "logged_hours"} else "TEXT"
                    )
                    columns.append(f"{field} {field_type}")
                conn.execute(text(f"CREATE TABLE {table} ({', '.join(columns)})"))
        return engine

    def setUp(self) -> None:
        self.engine = self.make_engine()
        self.metadata = {}
        for patcher in (
            patch.object(db, "get_engine", return_value=self.engine),
            patch.object(db, "_read_web_metadata", side_effect=lambda: self.metadata),
            patch.object(db, "_write_web_metadata", side_effect=self.save_metadata),
            patch.object(db, "_TABLES_MISSING_COLUMNS", {}),
            patch.object(db.task_events, "capability", return_value=db.task_events.Capability(False, "Disabled in synthetic fixture")),
            patch.object(db, "_assert_task_unblocked"),
            patch.object(db, "_spawn_follow_ups", return_value=[]),
            patch.dict(os.environ, {"LUIGI_WEB_RECURRENCE_OWNER": "web", "LUIGI_WEB_TIMEZONE": "UTC"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.source = {
            "uuid": "example-parent", "task": "Example scheduled task", "priority": 4,
            "status": "Completed", "completed": 1,
            "completed_time": "2030-04-10T12:00:00+00:00", "task_creation": "2030-04-01",
            "start_time": "2030-04-09T10:00:00", "due_date": "2030-04-10",
            "logged_hours": 2, "estimated_time": 1, "recurring": 1,
            "recurring_interval": 7, "recurring_days": None,
            "recurring_month_ordinal": None, "recurring_month_weekday": None,
            "project": "Example project", "catagory": "Example category",
            "task_group": "Example group", "sub_group": "Example subgroup",
            "relevant_link": "https://example.invalid/", "archived": 0,
        }
        self.insert(self.source)

    def save_metadata(self, data) -> None:
        self.metadata = data

    def use_missing_columns(self, missing_fields) -> None:
        self.engine = self.make_engine(missing_fields)
        patcher = patch.object(db, "get_engine", return_value=self.engine)
        patcher.start()
        self.addCleanup(patcher.stop)
        db._TABLES_MISSING_COLUMNS["recurring_tasks"] = set(missing_fields)
        self.insert({field: value for field, value in self.source.items() if field not in missing_fields})

    def insert(self, row) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(
                f"INSERT INTO recurring_tasks ({', '.join(row)}) "
                f"VALUES ({', '.join(':' + field for field in row)})"
            ), row)

    def rows(self, table="recurring_tasks") -> list[dict]:
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(text(f"SELECT * FROM {table}")).mappings()]

    def generate(self, today=date(2030, 4, 17)) -> int:
        return db.reactivate_due_recurring(today)

    def child(self) -> dict:
        return next(row for row in self.rows() if row["uuid"] != self.source["uuid"])

    def test_generation_preserves_every_source_field_and_resets_child(self) -> None:
        before = self.rows()[0]
        self.assertEqual(self.generate(), 1)
        rows = self.rows()
        self.assertEqual(rows[0], before)
        child = self.child()
        expected = occurrences.occurrence_payload(self.source, "2030-04-17", child["task_creation"])
        self.assertEqual({field: child[field] for field in expected}, expected)
        self.assertNotEqual(child["id"], before["id"])
        self.assertEqual(self.generate(), 0)
        self.assertEqual(self.generate(date(2031, 1, 1)), 0)
        self.assertEqual(self.rows(), rows)
        self.assertEqual(len(self.rows(occurrences.TABLE_NAME)), 1)

    def test_future_inactive_archived_and_inconsistent_status_do_not_generate(self) -> None:
        self.assertEqual(self.generate(date(2030, 4, 16)), 0)
        for field, value in (("recurring", 0), ("archived", 1), ("completed", 0), ("status", "Not Started")):
            with self.subTest(field=field):
                with self.engine.begin() as conn:
                    conn.execute(text(f"UPDATE recurring_tasks SET {field} = :value"), {"value": value})
                self.assertEqual(self.generate(), 0)
                with self.engine.begin() as conn:
                    conn.execute(text(f"UPDATE recurring_tasks SET {field} = :value"), {"value": self.source[field]})
        self.assertEqual(len(self.rows()), 1)

    def test_concurrent_workers_commit_only_one_child(self) -> None:
        barrier = Barrier(4)

        def generate_once():
            barrier.wait(timeout=10)
            return self.generate()

        with ThreadPoolExecutor(max_workers=4) as workers:
            counts = list(workers.map(lambda unused: generate_once(), range(4)))
        self.assertEqual(sorted(counts), [0, 0, 0, 1])
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len(self.rows(occurrences.TABLE_NAME)), 1)

    def test_deleted_child_never_respawns(self) -> None:
        self.generate()
        with patch("luigi_web.modules.tasks.operations.delete_task_records", return_value={}):
            db.delete_recurring(self.child()["uuid"])
        self.assertEqual(self.generate(), 0)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.rows(occurrences.TABLE_NAME)), 1)

    def test_child_insert_failure_rolls_back_ledger_and_source(self) -> None:
        with occurrences.transaction(self.engine) as conn:
            occurrences.ensure_storage(conn)
        before = self.rows()

        def fail_child(conn, cursor, statement, parameters, context, executemany):
            if statement.strip().startswith("INSERT INTO recurring_tasks"):
                raise OperationalError("synthetic insert", {}, Exception("synthetic failure"))

        event.listen(self.engine, "before_cursor_execute", fail_child)
        try:
            with self.assertRaisesRegex(occurrences.OccurrenceStorageError, "^Recurring occurrence storage is unavailable$"):
                self.generate()
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_child)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.rows(occurrences.TABLE_NAME), [])
        self.assertEqual(self.generate(), 1)

    def test_unrelated_unique_collision_is_not_reported_as_already_generated(self) -> None:
        child = occurrences.occurrence_payload(self.source, "2030-04-17", "2030-04-17T08:00:00")
        self.insert(child)
        before = self.rows()
        with self.assertRaises(occurrences.OccurrenceStorageError):
            self.generate()
        self.assertEqual(self.rows(), before)

    def test_reads_before_ledger_exists_have_no_annotations_or_ddl(self) -> None:
        statements = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            parent = db.get_recurring(self.source["uuid"])
            self.assertNotIn("_recurrence_generated", parent)
            self.assertEqual(len(db.list_recurring()), 1)
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        self.assertFalse(any("CREATE " in statement.upper() for statement in statements))

    def test_lineage_is_exposed_by_get_list_and_calendar_reads(self) -> None:
        self.generate()
        child_uuid = self.child()["uuid"]
        parent = db.get_recurring(self.source["uuid"])
        child = db.get_recurring(child_uuid)
        self.assertTrue(parent["_recurrence_generated"])
        self.assertEqual(parent["_recurrence_next_uuid"], child_uuid)
        self.assertIsNone(db.reactivation_date(parent))
        self.assertEqual(db.reactivation_date(self.source), "2030-04-17")
        self.assertEqual(child["_recurrence_parent_uuid"], self.source["uuid"])
        self.assertEqual(child["_recurrence_series_uuid"], self.source["uuid"])
        self.assertFalse(child["_recurrence_generated"])
        for rows in (db.list_recurring(), db.list_calendar_rows(date(2030, 4, 1), date(2030, 4, 30))):
            self.assertEqual(len(rows), 2)
            self.assertTrue(next(row for row in rows if row["uuid"] == self.source["uuid"])["_recurrence_generated"])
            self.assertEqual(next(row for row in rows if row["uuid"] == child_uuid)["_recurrence_parent_uuid"], self.source["uuid"])

    def test_fallback_metadata_snapshot_and_explicit_clears(self) -> None:
        missing = set(occurrences.METADATA_FIELDS)
        self.use_missing_columns(missing)
        self.metadata = {"recurring_tasks": {self.source["uuid"]: {
            "project": "Example inherited project", "recurring_days": "0,4",
            "recurring_month_ordinal": -1, "recurring_month_weekday": 4,
        }}}
        self.assertEqual(self.generate(date(2030, 4, 26)), 1)
        child_uuid = self.child()["uuid"]
        child = db.get_recurring(child_uuid)
        self.assertEqual(child["project"], "Example inherited project")
        self.assertEqual(child["recurring_days"], "0,4")
        self.assertEqual(child["recurring_month_ordinal"], -1)
        self.assertEqual(child["recurring_month_weekday"], 4)
        self.assertEqual(child["due_date"], "2030-04-26")
        db._set_web_metadata("recurring_tasks", child_uuid, project=None, recurring_days=None, recurring_month_ordinal=None, recurring_month_weekday=None)
        cleared = db.get_recurring(child_uuid)
        for field in missing - {"archived"}:
            self.assertIsNone(cleared[field])
        self.assertIsNone(next(row for row in db.list_calendar_rows(date(2030, 4, 1), date(2030, 4, 30)) if row["uuid"] == child_uuid)["project"])

    def test_fallback_edits_are_guarded_and_child_clears_survive_next_generation(self) -> None:
        self.use_missing_columns(occurrences.METADATA_FIELDS)
        self.metadata = {"recurring_tasks": {self.source["uuid"]: {
            "project": "Example inherited project", "recurring_days": "0,4",
        }}}
        self.generate()
        original_metadata = {key: dict(value) for key, value in self.metadata["recurring_tasks"].items()}
        with self.assertRaisesRegex(ValueError, "^This occurrence has a successor"):
            db.update_recurring(self.source["uuid"], {"project": None, "recurring": 1, "recurring_schedule_type": "interval", "recurring_interval": 3})
        self.assertEqual(self.metadata["recurring_tasks"], original_metadata)
        child_uuid = self.child()["uuid"]
        db.update_recurring(child_uuid, {"project": None, "recurring": 1, "recurring_schedule_type": "interval", "recurring_interval": 3})
        child = db.get_recurring(child_uuid)
        self.assertIsNone(child["project"])
        self.assertIsNone(child["recurring_days"])
        self.assertEqual(child["recurring_interval"], 3)
        with patch.object(db, "now_iso", return_value="2030-04-17T12:00:00+00:00"):
            db.set_recurring_status(child_uuid, "Completed")
        self.assertEqual(self.generate(date(2030, 4, 20)), 1)
        grandchild = next(row for row in db.list_recurring() if not row["completed"])
        self.assertIsNone(grandchild["project"])
        self.assertIsNone(grandchild["recurring_days"])
        self.assertEqual(grandchild["due_date"], "2030-04-20")

    def test_fallback_archive_is_seen_by_scheduler_and_undo(self) -> None:
        self.use_missing_columns({"archived"})
        snapshot = db.get_recurring(self.source["uuid"])
        self.assertTrue(db.archive_recurring(self.source["uuid"]))
        self.assertEqual(self.generate(), 0)
        db.restore_task_row("recurring_tasks", snapshot)
        self.assertEqual(self.generate(), 1)
        child_uuid = self.child()["uuid"]
        self.assertTrue(db.archive_recurring(child_uuid))
        self.assertEqual(self.generate(date(2031, 1, 1)), 0)

    def test_unrelated_ledger_child_unique_collision_rolls_back(self) -> None:
        child = occurrences.occurrence_payload(self.source, "2030-04-17", "2030-04-17T08:00:00")
        with occurrences.transaction(self.engine) as conn:
            occurrences.ensure_storage(conn)
            conn.execute(text(f"""
                INSERT INTO {occurrences.TABLE_NAME} (
                    parent_uuid, child_uuid, series_uuid, completed_at, due_date, generated_at
                ) VALUES (
                    'example-other-parent', :child_uuid, 'example-other-series',
                    '2030-04-10T12:00:00', '2030-04-17', '2030-04-17T08:00:00'
                )
            """), {"child_uuid": child["uuid"]})
        before = self.rows()
        links = self.rows(occurrences.TABLE_NAME)
        with self.assertRaises(occurrences.OccurrenceStorageError):
            self.generate()
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.rows(occurrences.TABLE_NAME), links)

    def test_lineage_reads_are_batched(self) -> None:
        self.generate()
        statements = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            if "FROM " + occurrences.TABLE_NAME in statement:
                statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            self.assertEqual(len(db.list_recurring()), 2)
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        self.assertEqual(len(statements), 1)

    def test_generated_parent_cannot_be_reopened_edited_or_snoozed(self) -> None:
        self.generate()
        before = self.rows()
        writes = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            if statement.strip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
                writes.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            mutations = (
                lambda: db.set_recurring_status(self.source["uuid"], "Not Started"),
                lambda: db.toggle_recurring_completed(self.source["uuid"]),
                lambda: db.update_recurring(self.source["uuid"], {"recurring": 1, "recurring_interval": 3}),
                lambda: db.snooze_recurring(self.source["uuid"], 1),
                lambda: db.set_recurring_metadata(self.source["uuid"], field="project", value="Example changed project"),
                lambda: db.restore_task_row("recurring_tasks", {**self.source, "completed": 0, "completed_time": None, "status": "Not Started"}, ["example-follow-up"]),
            )
            for mutate in mutations:
                with self.subTest(mutate=mutate), self.assertRaisesRegex(ValueError, "^This occurrence has a successor"):
                    mutate()
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        self.assertEqual(writes, [])
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.metadata, {})

    def test_repeated_completed_status_preserves_timestamp_before_and_after_generation(self) -> None:
        before = self.rows()[0]
        self.assertEqual(db.set_recurring_status(self.source["uuid"], "Completed").completed, 1)
        self.assertEqual(self.rows()[0], before)
        self.generate()
        self.assertEqual(db.set_recurring_status(self.source["uuid"], "Completed").completed, 1)
        self.assertEqual(self.rows()[0], before)

    def test_undo_before_generation_works_and_prevents_generation(self) -> None:
        snapshot = {**self.source, "completed": 0, "completed_time": None, "status": "Not Started"}
        db.restore_task_row("recurring_tasks", snapshot)
        self.assertEqual(self.generate(), 0)
        self.assertEqual(self.rows()[0]["completed"], 0)

    def test_generated_history_guards_persist_under_external_ownership(self) -> None:
        self.generate()
        with patch.dict(os.environ, {"LUIGI_WEB_RECURRENCE_OWNER": "external"}):
            with self.assertRaisesRegex(ValueError, "^This occurrence has a successor"):
                db.toggle_recurring_completed(self.source["uuid"])
            self.assertTrue(db.get_recurring(self.source["uuid"])["_recurrence_generated"])
            self.assertEqual(self.generate(), 0)

    def test_archive_and_matching_completed_restore_are_allowed(self) -> None:
        self.generate()
        before = self.rows()[0]
        self.assertTrue(db.archive_recurring(self.source["uuid"]))
        db.restore_task_row("recurring_tasks", self.source)
        self.assertEqual(self.rows()[0], before)
        self.assertTrue(db.get_recurring(self.source["uuid"])["_recurrence_generated"])
        with self.assertRaisesRegex(ValueError, "^This occurrence has a successor"):
            db.restore_task_row("recurring_tasks", {**self.source, "recurring_interval": 2})

    def test_deleted_parent_restore_requires_matching_completed_history(self) -> None:
        self.generate()
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM recurring_tasks WHERE uuid = :uuid"), {"uuid": self.source["uuid"]})
        with self.assertRaisesRegex(ValueError, "^This occurrence has a successor"):
            db.restore_task_row("recurring_tasks", {**self.source, "completed_time": "2030-04-11T12:00:00+00:00"})
        for changes in (
            {"recurring_interval": 2}, {"task": "Example changed historical label"},
            {"logged_hours": 9}, {"due_date": "2030-04-11"},
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "^This occurrence has a successor"):
                db.restore_task_row("recurring_tasks", {**self.source, **changes})
        db.restore_task_row("recurring_tasks", self.source)
        self.assertEqual(self.generate(), 0)
        self.assertEqual(len(self.rows()), 2)

    def test_multiple_generations_keep_one_open_child_and_original_series(self) -> None:
        self.generate()
        child = self.child()
        with patch.object(db, "now_iso", return_value="2030-04-17T12:00:00+00:00"):
            db.set_recurring_status(child["uuid"], "Completed")
        history = self.rows()
        self.assertEqual(self.generate(date(2030, 4, 24)), 1)
        self.assertEqual(self.rows()[:2], history)
        rows = db.list_recurring()
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(not row["completed"] for row in rows), 1)
        for row in rows:
            self.assertEqual(row["_recurrence_series_uuid"], self.source["uuid"])
        middle = next(row for row in rows if row["uuid"] == child["uuid"])
        self.assertEqual(middle["_recurrence_parent_uuid"], self.source["uuid"])
        self.assertTrue(middle["_recurrence_generated"])
        self.assertEqual(self.generate(date(2031, 1, 1)), 0)

    def test_schedule_variants_reuse_existing_date_math(self) -> None:
        weekday = {**self.source, "uuid": "example-weekday", "recurring_interval": None, "recurring_days": "0,4"}
        monthly = {**self.source, "uuid": "example-monthly", "recurring_interval": None, "recurring_month_ordinal": -1, "recurring_month_weekday": 4}
        unscheduled = {**self.source, "uuid": "example-unscheduled", "recurring_interval": None}
        for source in (weekday, monthly, unscheduled):
            self.insert(source)
        before = {row["uuid"]: row for row in self.rows()}
        self.assertEqual(self.generate(date(2030, 4, 26)), 3)
        due_dates = {row["parent_uuid"]: row["due_date"] for row in self.rows(occurrences.TABLE_NAME)}
        self.assertEqual(due_dates, {
            self.source["uuid"]: "2030-04-17", "example-weekday": "2030-04-12",
            "example-monthly": "2030-04-26",
        })
        for row in self.rows():
            if row["uuid"] in before:
                self.assertEqual(row, before[row["uuid"]])

    def test_effective_completion_override_controls_due_date(self) -> None:
        with patch.object(db.task_events, "capability", return_value=db.task_events.Capability(True, "Synthetic event override")), patch.object(db.task_events, "latest_active_completion_effective_date", return_value="2030-04-09") as effective_date:
            self.assertEqual(self.generate(date(2030, 4, 16)), 1)
        self.assertEqual(self.child()["due_date"], "2030-04-16")
        self.assertEqual(self.rows()[0]["completed_time"], self.source["completed_time"])
        self.assertEqual(effective_date.call_args.kwargs["source_task_uuid"], self.source["uuid"])

    def test_timezone_and_default_today_are_used(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE recurring_tasks SET completed_time = '2030-04-11T01:00:00+00:00'"))
        with patch.object(db.clock, "user_timezone", return_value=ZoneInfo("America/New_York")), patch.object(db.clock, "local_today", return_value=date(2030, 4, 17)):
            self.assertEqual(db.reactivate_due_recurring(), 1)
        self.assertEqual(self.child()["due_date"], "2030-04-17")

    def test_storage_permission_failures_are_generic_and_leave_no_partial_writes(self) -> None:
        with occurrences.transaction(self.engine) as conn:
            occurrences.ensure_storage(conn)
        before = self.rows()
        for denied in (
            "CREATE TABLE IF NOT EXISTS " + occurrences.TABLE_NAME,
            "FROM " + occurrences.TABLE_NAME,
            "INSERT INTO " + occurrences.TABLE_NAME,
        ):
            def fail_storage(conn, cursor, statement, parameters, context, executemany):
                if denied in statement:
                    raise OperationalError("synthetic statement", {}, Exception("synthetic permission denial"))

            with self.subTest(denied=denied):
                event.listen(self.engine, "before_cursor_execute", fail_storage)
                try:
                    with self.assertRaisesRegex(occurrences.OccurrenceStorageError, "^Recurring occurrence storage is unavailable$"):
                        self.generate()
                finally:
                    event.remove(self.engine, "before_cursor_execute", fail_storage)
                self.assertEqual(self.rows(), before)
                self.assertEqual(self.rows(occurrences.TABLE_NAME), [])
        self.assertEqual(self.generate(), 1)

    def test_commit_failure_does_not_report_success_or_persist_child(self) -> None:
        with occurrences.transaction(self.engine) as conn:
            occurrences.ensure_storage(conn)
        before = self.rows()

        def fail_commit(conn):
            raise OperationalError("synthetic commit", {}, Exception("synthetic failure"))

        event.listen(self.engine, "commit", fail_commit)
        try:
            with self.assertRaises(occurrences.OccurrenceStorageError):
                self.generate()
        finally:
            event.remove(self.engine, "commit", fail_commit)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.rows(occurrences.TABLE_NAME), [])

    def test_readback_mismatch_rolls_back_child_and_ledger(self) -> None:
        with occurrences.transaction(self.engine) as conn:
            occurrences.ensure_storage(conn)
        before = self.rows()
        for table in ("recurring_tasks", occurrences.TABLE_NAME):
            def change_insert(conn, cursor, statement, parameters, context, executemany):
                if statement.strip().startswith("INSERT INTO " + table):
                    conn.execute(text(f"UPDATE {table} SET due_date = '2030-01-01'"))

            with self.subTest(table=table):
                event.listen(self.engine, "after_cursor_execute", change_insert)
                try:
                    with self.assertRaises(occurrences.OccurrenceStorageError):
                        self.generate()
                finally:
                    event.remove(self.engine, "after_cursor_execute", change_insert)
                self.assertEqual(self.rows(), before)
                self.assertEqual(self.rows(occurrences.TABLE_NAME), [])

    def test_late_failure_rolls_back_previously_generated_children(self) -> None:
        self.insert({**self.source, "uuid": "example-second-parent"})
        with occurrences.transaction(self.engine) as conn:
            occurrences.ensure_storage(conn)
        before = self.rows()
        insert_count = 0

        def fail_second_child(conn, cursor, statement, parameters, context, executemany):
            nonlocal insert_count
            if statement.strip().startswith("INSERT INTO recurring_tasks"):
                insert_count += 1
                if insert_count == 2:
                    raise OperationalError("synthetic insert", {}, Exception("synthetic failure"))

        event.listen(self.engine, "before_cursor_execute", fail_second_child)
        try:
            with self.assertRaises(occurrences.OccurrenceStorageError):
                self.generate()
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_second_child)
        self.assertEqual(insert_count, 2)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.rows(occurrences.TABLE_NAME), [])

    def test_candidates_are_reread_before_generation(self) -> None:
        for field, value in (
            ("completed", 0), ("status", "Not Started"), ("archived", 1),
            ("recurring", 0), ("recurring_interval", 30),
            ("completed_time", "2030-04-20T12:00:00+00:00"),
        ):
            changed = False

            def change_candidate(conn, cursor, statement, parameters, context, executemany):
                nonlocal changed
                if statement.strip().startswith("SELECT uuid FROM recurring_tasks"):
                    conn.execute(text(f"UPDATE recurring_tasks SET {field} = :value"), {"value": value})
                    changed = True

            with self.subTest(field=field):
                event.listen(self.engine, "after_cursor_execute", change_candidate)
                try:
                    self.assertEqual(self.generate(), 0)
                finally:
                    event.remove(self.engine, "after_cursor_execute", change_candidate)
                self.assertTrue(changed)
                self.assertEqual(len(self.rows()), 1)
                self.assertEqual(self.rows(occurrences.TABLE_NAME), [])
                with self.engine.begin() as conn:
                    conn.execute(text(f"UPDATE recurring_tasks SET {field} = :value"), {"value": self.source[field]})

    def test_reopen_and_generation_share_the_write_lock(self) -> None:
        barrier = Barrier(2)

        def generate_once():
            barrier.wait(timeout=10)
            return self.generate()

        def reopen_once():
            barrier.wait(timeout=10)
            try:
                db.set_recurring_status(self.source["uuid"], "Not Started")
            except ValueError as error:
                self.assertTrue(str(error).startswith("This occurrence has a successor"))
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as workers:
            generated = workers.submit(generate_once)
            reopened = workers.submit(reopen_once)
            count, was_reopened = generated.result(), reopened.result()
        self.assertEqual(count, 0 if was_reopened else 1)
        self.assertEqual(len(self.rows()), 1 if was_reopened else 2)
        self.assertEqual(self.rows()[0]["completed"], 0 if was_reopened else 1)

    def test_archived_child_and_recompleted_source_do_not_fan_out(self) -> None:
        self.generate()
        db.archive_recurring(self.child()["uuid"])
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE recurring_tasks SET completed_time = '2030-04-11T12:00:00+00:00' WHERE uuid = :uuid"), {"uuid": self.source["uuid"]})
        self.assertEqual(self.generate(date(2031, 1, 1)), 0)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len(self.rows(occurrences.TABLE_NAME)), 1)

    def test_generation_ddl_is_only_web_ledger_and_insert_omits_identity(self) -> None:
        statements = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement.strip())

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            self.generate()
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        ddl = [statement for statement in statements if statement.split()[0] in {"CREATE", "ALTER", "DROP"}]
        self.assertEqual(len(ddl), 1)
        self.assertTrue(ddl[0].startswith("CREATE TABLE IF NOT EXISTS " + occurrences.TABLE_NAME))
        self.assertFalse(any("schema_version" in statement for statement in statements))
        self.assertFalse(any(statement.startswith("UPDATE recurring_tasks") for statement in statements))
        insert = next(statement for statement in statements if statement.startswith("INSERT INTO recurring_tasks"))
        columns = insert.split("(", 1)[1].split(")", 1)[0].replace(" ", "").split(",")
        self.assertNotIn("id", columns)


if __name__ == "__main__":
    unittest.main()