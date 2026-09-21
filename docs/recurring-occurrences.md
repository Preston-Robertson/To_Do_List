# Recurring Occurrences

Scheduled recurrence can create a new task for each occurrence while retaining
the completed task and its history. This is implemented behind a deployment
opt-in; installing the update does **not** automatically activate it in production.
It is separate from one-off completion triggers and the proposed Automation UI.

## Ownership and upgrade compatibility

`LUIGI_WEB_RECURRENCE_OWNER` accepts exactly these values:

| Value | Behavior |
|---|---|
| `external` (default) | No web scheduled generation, no old web reset, and no creation of the occurrence ledger by the scheduler. Handling remains external. |
| `web` | Generate due successors using the web-owned ledger, only after a coordinated deployment handoff. |

**Existing web-only installations must now opt in.** The previous implicit web
reactivation behavior is removed even when no LuigiBot scheduler is running.
Set `web` only after disabling/coordinating LuigiBot's legacy reset scheduler
and any other writer that automatically reopens the same recurring rows.
Luigi Web does not detect or disable another scheduler. `external` does not
implement copy behavior in LuigiBot or guarantee history if the bot still resets
rows. Do not run the legacy reset and web copy schedulers together.

Ownership belongs in the deployment's process environment, with changes applied
through the normal service restart. It is not in the Admin `KNOWN_KEYS` allow-list
and there is no GUI ownership toggle. Any displayed ownership state describes
configuration, not proof that LuigiBot has stopped. See the commented default in
[../.env.example](../.env.example); do not put real deployment values in this repo.

Returning to `external` stops new web generation. It does not remove existing
children, ledger entries, or the web guards on completed history. It is not a
data rollback or permission to resume a legacy reset scheduler on those rows.

## When a child is created

Generation runs on startup when Tasks is enabled and on existing task/Home read
paths, not on a background timer or dedicated worker. The compatibility function
is still named `reactivate_due_recurring`, but no longer resets a completed row.
A candidate must be unarchived, recurring, completed with status `Completed`,
have a valid completion/schedule, and have no recorded successor.

The next due date is strictly after completion, using existing schedule math:

1. A valid monthly position takes precedence, such as the first Monday or last
   Friday strictly after the completion date.
2. Otherwise, selected weekdays choose the next matching day strictly after
   completion, not the same day.
3. Otherwise, a positive interval adds that many days to completion.

When the shared completion-event ledger is available, its latest active effective
completion date is used if present. Otherwise the saved `completed_time` is
converted to the configured local date. The sweep compares the due date against
`clock.local_today()` in `LUIGI_WEB_TIMEZONE`; it does not rewrite the parent's
actual completion timestamp. Explicit completion-date overrides still require
LuigiBot's optional `task_events` migration.

Only one due successor is created per parent, once its calculated date is today
or earlier. A delayed/offline deployment can create a child that is already
overdue. It does not fill every missed calendar date, jump to a future date, or
recursively generate a chain in one sweep. The next child requires this child's
own later completion. Calendar projections remain read-only date projections.

For example, an `Example scheduled task` completed on 2030-04-10 with a seven-day
interval gets one child due 2030-04-17, even if the next sweep is on 2030-04-30.
The completed 2030-04-10 occurrence is not reopened.

## Identity and copied fields

Both parent and child are ordinary rows in the shared `recurring_tasks` table.
Generation leaves every parent field unchanged, including its ID/UUID, status,
completion timestamp, original due date, schedule, and logged time.

The child gets a deterministic UUID5 derived from its parent's UUID and saved
completion timestamp. Retries use that same identity; a new parent completion
has a different identity. The database assigns a fresh row ID. The task name,
priority, link, `catagory`, group/subgroup, estimate, project, recurring flag,
interval, weekday selection, and monthly-position definition are copied.

Execution state is fresh: `status="Not Started"`, `completed=0`,
`completed_time=None`, `start_time=None`, `logged_hours=0`, and `archived=0`.
`task_creation` is the generation time and `due_date` is the calculated date.
The current occurrence carries the rule; no separate template table or automatic
series fork is implemented. Edits to the current mutable occurrence affect its
next successor, not earlier completed snapshots.

## Ledger and permissions

`luigi_web_recurring_occurrences` is an app-owned PostgreSQL table in the **same
database** as `recurring_tasks`, not in Finance or another SQLite database.

| Column | Contract |
|---|---|
| `parent_uuid` | Text primary key; at most one consumed generation per parent |
| `child_uuid` | Non-null unique text; the generated occurrence |
| `series_uuid` | Non-null text; stable initial root UUID through later generations |
| `completed_at` | Non-null text; saved parent completion timestamp |
| `due_date` | Non-null text; calculated child due date |
| `generated_at` | Non-null text; child creation timestamp |
| `metadata_json` | Nullable text JSON; optional child metadata and `_parent_snapshot` |

Child metadata includes project/archive and weekday/monthly fields when optional
shared columns are unavailable. The parent snapshot records the completed task
fields for history guards. Existing metadata fallbacks still support mutable
occurrence edits; generation itself needs no separate metadata-file commit.

Initialization uses `CREATE TABLE IF NOT EXISTS` only for this app-owned ledger.
It does not alter shared tables, bump LuigiBot's `schema_version`, or perform
destructive DDL. Existing optional web-column initialization is a separate path.
The runtime role needs schema access and its existing shared-task permissions,
plus `SELECT` and `INSERT` on the ledger for the current read/generation paths.
First creation requires `CREATE` in the target schema, or a compatible table
pre-provisioned by its owner with those grants. No ledger `UPDATE` or `DELETE`
is used by the current occurrence code. Retain ledger read access after switching
to `external`, because lineage and history guards still use it. Do not grant
broad ownership of LuigiBot's shared schema just to enable this feature.

Child and ledger inserts, their readback verification, and commit share one
PostgreSQL transaction. A failed insert, verification, or commit must not report
a generated child. PostgreSQL locks the source row with `FOR UPDATE`; the
parent primary key, child uniqueness, and deterministic UUID make repeated or
concurrent sweeps idempotent. First-time PostgreSQL ledger creation also takes
an advisory transaction lock. There is no cross-database commit dependency.

## Corrections and stopping a series

Before generation, normal completion correction or the existing short Undo
window can return a source to incomplete, preventing a successor from that
completion. Once a child has been generated, Luigi Web rejects parent edits,
reopening, snoozing, and Undo that would change completed history or return it
to incomplete. These guards remain after switching ownership to `external`;
they do not constrain an independently running bot or direct database writer.

Explicit archive and delete remain supported. Archive can hide history and
delete can remove it; neither is automatic recurrence cleanup. A matching
completed snapshot can be restored without rewriting its historical fields.
Deleting or archiving a child does not reset its parent's consumed ledger entry
and does not make another child appear. Disable Repeat or archive the current
mutable occurrence to stop its future generation. Do not delete ledger entries
to retry or restart a series: that can cause duplicates. A deliberately new
recurring task starts a separate series rather than rewriting an old parent.

## Adopting existing rows

There is no bulk conversion or historical backfill. An existing unlinked row
becomes an initial series root when it is completed and due during a web-owned
sweep; its own UUID becomes the series UUID. Future or incomplete rows wait for
their normal eligibility. History already overwritten by previous in-place
resets cannot be reconstructed. Plan the scheduler handoff and a recoverable
backup before opting in, including for an installation previously using only
the web scheduler.

## Backup and recovery

**Back up both `recurring_tasks` and `luigi_web_recurring_occurrences`.** Keep
them in the same consistent PostgreSQL backup/recovery point, including any
applicable shared completion-event history. Preserve the deployment's existing
web metadata fallback as well; generation snapshots do not replace every later
metadata edit.

The normal Admin shared-task JSON export/merge restore currently covers the five
LuigiBot tables and web metadata, **not this ledger**. It is not a complete
recurrence disaster-recovery mechanism. Do not restore/reset an older recurring
copy without its matching ledger. Task-only restore rejects rows and metadata
for completed parents that already have successors, both at preview and again
under the restore transaction's locks. It does not export or reconstruct the
ledger. Missing or mismatched lineage can lose history
protection, permit duplicates, or cause generation to fail. Coordinate full
database recovery with the database owner while scheduling is stopped; keep
backups private and outside the repository. Ownership changes never erase data.

## Local demonstration

The disposable workspace helper can demonstrate completed history and a fresh
successor without touching shared storage:

```powershell
python scripts/preview_workspace.py --occurrence-demo
```

Open Tasks in the printed loopback URL. The two `Example recurring occurrence`
rows have different IDs: the completed parent has a read-only history view and
the open child can be edited. The preview uses an in-memory occurrence ledger;
its web-owner setting does not enable or verify production scheduling.

## Validation and source

In an isolated development environment with synthetic fixtures and no production
credentials, the focused checks and full regression entry point are:

```powershell
python -m unittest discover -s tests -p "test_occurrence_scheduler.py" -v
python -m unittest discover -s tests -p "test_recurring_occurrences.py" -v
python -m unittest discover -s tests -v
python scripts/validate_repo.py
git diff --check
```

See [../README.md](../README.md#development) for development prerequisites.
The scheduler suite uses temporary SQLite storage, including a four-worker
concurrency test; it does not establish PostgreSQL lock, DDL-permission, or
deployment behavior. PostgreSQL integration validation has **not** been run in
this documentation pass. No production queries, migrations, or deployment are
part of these documentation changes, and no test-pass totals are claimed here.

The contract is defined by
[../module-repos/tasks/src/luigi_web/modules/tasks/occurrences.py](../module-repos/tasks/src/luigi_web/modules/tasks/occurrences.py)
and [../module-repos/tasks/src/luigi_web/modules/tasks/repository.py](../module-repos/tasks/src/luigi_web/modules/tasks/repository.py).
Focused fixtures are in
[../tests/test_occurrence_scheduler.py](../tests/test_occurrence_scheduler.py)
and [../tests/test_recurring_occurrences.py](../tests/test_recurring_occurrences.py).
