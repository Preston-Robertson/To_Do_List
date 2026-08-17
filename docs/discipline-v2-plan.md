# Discipline v2 — coordinated LuigiBot/web plan

## Source of truth and decision

Discipline remains shared between this GUI and the shared LuigiBot repository.
LuigiBot owns the
database schema and migrations in `bot_modules/db.py`; this web repository must
not independently invent or deploy a competing Discipline schema.

The verified LuigiBot implementation currently has:

- `SCHEMA_VERSION = 2` and an idempotent `init_db()` migration path;
- SQLite and PostgreSQL support behind one SQLAlchemy Core API;
- a UUID-bearing `discipline_list` table included in `_UUID_TABLES`;
- `discipline_completions` as the long-format source of truth, keyed by
  `UNIQUE(task, completed_date)` and intentionally excluded from `_UUID_TABLES`;
- a derived wide history matrix rebuilt by `read_discipline_history()`;
- Pandas-facing compatibility APIs used throughout `main.py`,
  `discipline_helpers.py`, chart rendering, and Discord UI components.

The target is therefore **Discipline v2 on LuigiBot schema version 3**:
preserve the existing UUID-keyed definitions, replace task-text identity for
completion events, and keep the bot's DataFrame/chart API compatible during
the cutover.

## Why the current model fails

The current completion identity is `(task text, completed_date)` while the
definition already has a stable UUID. Consequences:

- rename, case, or whitespace changes can detach history;
- every writer must reproduce the exact same task text;
- `current_streak` is separately persisted even though all streak/report data
  can be derived from the completion log;
- the web role may be allowed to insert a completion but not update
  `discipline_list.current_streak`;
- the GUI and Discord bot can each report success from different partial
  portions of one logical action.

The Admin **Discipline storage** health card now checks the legacy columns and
INSERT/DELETE/streak-UPDATE permissions inside a transaction that is always
rolled back. This diagnoses the immediate production failure without leaving
test rows, but it does not make text-key storage safe.

## Critical LuigiBot writer constraint

`bot_modules.db.save_discipline_df()` currently performs a whole-table replace:

1. `DELETE FROM discipline_list`
2. reinsert every Pandas row, preserving UUID values carried in the DataFrame

That behavior is MVCC-safe for readers, but it is incompatible with a child
table foreign key to `discipline_list.uuid`: deleting definitions would either
fail or cascade away completion history. **The first delivery step must refactor
this writer to UUID-scoped upserts before any foreign key is introduced.**

The public DataFrame function may remain for caller compatibility, but its SQL
implementation must:

- upsert rows by UUID;
- add new UUIDs;
- update renamed/category/frequency/active fields in place;
- deactivate or explicitly remove omitted rows according to a documented rule;
- never delete and reinsert unchanged definitions.

The bot's existing `_smoke_uuid.py` round-trip tests should be extended to
prove UUIDs and completion events survive repeated DataFrame saves.

## Target schema

Reuse the existing `discipline_list` table. Do not duplicate definitions into
a second table.

### Existing `discipline_list` (retained)

| Column | Role after cutover |
|---|---|
| `uuid TEXT` | Stable shared identity; unique index already created by schema v2 |
| `task TEXT NOT NULL` | Mutable display name only |
| `catagory TEXT` | Existing intentional spelling |
| `frequency_per_week INTEGER` | Weekly target |
| `active INTEGER` | Existing 0/1 convention |
| `current_streak INTEGER` | Compatibility cache only; never source of truth |

### New `discipline_events_v3`

| Column | Purpose |
|---|---|
| `id` | Dialect-specific identity primary key (`IDENTITY` on PostgreSQL, autoincrement on SQLite) |
| `discipline_uuid TEXT NOT NULL` | Stable identity of the definition |
| `completed_date TEXT NOT NULL` | Bare `YYYY-MM-DD` |
| `logged_at TEXT NOT NULL` | ISO timestamp |
| `source TEXT` | `discord`, `web`, or `migration` |
| `task_snapshot TEXT` | Optional audit display value; never used as identity |
| `catagory_snapshot TEXT` | Optional category-at-completion audit value |

Required constraints/indexes:

- `FOREIGN KEY (discipline_uuid) REFERENCES discipline_list(uuid)`;
- `UNIQUE (discipline_uuid, completed_date)`;
- index on `completed_date`;
- index on `discipline_uuid`.

A new table, rather than altering the legacy completion table in place, gives
SQLite and PostgreSQL the same foreign-key shape and permits a safe
compatibility period. `discipline_completions` remains untouched until final
cleanup approval.

## LuigiBot schema-v3 migration

Implement in `bot_modules/db.py`, following the existing pattern:

1. Increase `SCHEMA_VERSION` from `2` to `3`.
2. Add `_migrate_to_v3(conn, *, allow_ddl: bool)` beside `_migrate_to_v2`.
3. Add dialect-correct `CREATE TABLE discipline_events_v3` to schema setup.
4. Refuse clearly when PostgreSQL ownership blocks required DDL, using the
   existing `_MigrationBlocked` pattern.
5. Preserve the existing v1→v2 path, then run v2→v3; fresh databases should be
   created directly at v3.
6. Keep `_ensure_recurring_days_columns()` as its separate unversioned add-on.

### Legacy event mapping

For each `discipline_completions` row:

1. Normalize `task` only for migration matching (`strip` + case-fold).
2. Match against `discipline_list.task`.
3. Require exactly one UUID match.
4. Report zero-match and multi-match rows with task/date/category details.
5. Abort the migration transaction if any row is ambiguous; never guess.
6. Insert mapped events with `source='migration'` and
   `ON CONFLICT (discipline_uuid, completed_date) DO NOTHING`.
7. Compare total and per-discipline dates against legacy rows before setting
   `schema_version = 3`.

Ship a PostgreSQL owner script analogous to
`scripts/migrate_v1_to_v2.sql`, plus an automated Python migration path for
SQLite and owner-capable PostgreSQL deployments. Do not require the user to
assemble one-off SQL manually.

## LuigiBot adapter changes

### `bot_modules/db.py`

Add UUID-first functions:

- `append_discipline_event(discipline_uuid, completed_date, *, source,
  task_snapshot=None, catagory_snapshot=None)`;
- `delete_discipline_event(discipline_uuid, completed_date)`;
- `is_discipline_completed_on(discipline_uuid, completed_date)`;
- `load_discipline_event_df()`.

Retain legacy wrappers (`append_discipline_completion`,
`delete_discipline_completion`, `set_discipline_cell`,
`is_task_completed_on`) during one compatibility release. Wrappers must resolve
task text to exactly one current UUID and fail visibly on ambiguity.

Extend DataFrame mappings with `DISCIPLINE_UUID`, while projecting the current
definition name back into `TASK` so current chart and report helpers keep their
expected columns. `read_discipline_history()` should join events to
`discipline_list` by UUID before pivoting to the existing date × task matrix.

### `bot_modules/discipline_helpers.py`

Preserve current public helper names initially, but route mark/unmark/history
operations through UUID-first DB functions. Keep chart builders and weekly
analytics unchanged where possible by maintaining their DataFrame input shape.

### `main.py`

Verified call sites requiring conversion include:

- `create_discipline_task`: continue building a definition row, but save via
  the refactored UUID-upsert writer;
- `log_discipline_completion`: resolve the canonical definition once, retain
  its UUID, and append by UUID;
- `discipline_list`, `today_completions`, `weekly_discipline_report`,
  `discipline_streaks`, `discipline_progress`, `discipline_category_rollup`,
  `at_risk`, and `discipline_heatmap`: consume the UUID-joined event DataFrame;
- daily/weekly scheduled discipline reports: use the same adapter, not direct
  legacy completion queries.

Remove the command's `save_discipline_df()` call that persists
`CURRENT_STREAK` after logging. Streak displays should derive from events. The
column can remain populated as a compatibility cache for one release, but a
cache failure must never roll back a completion event.

### `bot_modules/ui_components.py`

`DisciplineTaskView` currently identifies items by task text. Carry the
definition UUID in button/select state and submit UUID + date to the adapter.
Display text remains the current task name.

## Web GUI cutover

- Require LuigiBot schema version 3 only when the v3 adapter is deployed; do
  not strand a schema-v2 production bot with a partially upgraded web client.
- Home and heatmap controls submit `discipline_uuid` and date only.
- Create/edit/deactivate remain UUID-scoped.
- Mark/unmark use one atomic event operation and re-read by UUID/date before
  returning success.
- Heatmaps and streaks query `discipline_events_v3` joined to definitions.
- Rename updates only `discipline_list.task`; no completion rows move.
- Backups and Integration Health include/verify `discipline_events_v3`, its
  unique constraint, FK, and rolled-back write access.
- Remove the web-side legacy task normalization fallback only after both
  clients are confirmed on v3.

## Test and rollout matrix

### LuigiBot automated tests

Extend the throwaway-SQLite smoke approach from `scripts/_smoke_uuid.py`:

1. fresh schema v3 creation and idempotent second init;
2. v2→v3 migration preserves all definitions/events and row counts;
3. ambiguous legacy task names abort with a useful report;
4. repeated `save_discipline_df()` does not delete events or change UUIDs;
5. rename preserves event history;
6. duplicate UUID/date insertion is ignored;
7. DataFrame projection still powers existing streak/report/chart helpers;
8. Discord UI callbacks carry UUIDs.

Run equivalent PostgreSQL integration tests for constraints and transaction
behavior; SQLite-only validation is insufficient for the shared deployment.

### Web tests

1. Home Done and heatmap mark/unmark persist by UUID;
2. rename leaves all dates/streaks visible;
3. duplicate clicks remain idempotent;
4. DB errors preserve truthful UI and show an actionable toast;
5. Integration Health rolls back its capability row;
6. command-palette discipline search opens the UUID-scoped definition.

### Deployment order

1. Back up current PostgreSQL tables and app metadata.
2. Deploy LuigiBot code containing schema v3, UUID adapters, migration report,
   and legacy wrappers—but leave legacy tables present.
3. Confirm bot commands and scheduled reports on v3.
4. Deploy the web v3 adapter.
5. Compare legacy/v3 per-discipline dates in Admin.
6. Run both clients through one compatibility release.
7. Remove legacy wrappers/table only in a separately approved later migration.

## Delivery constraints

- No manual SQL troubleshooting loop for the user.
- Migration, ambiguity reporting, backups, and diagnostics ship in code.
- No destructive legacy-table changes in the cutover release.
- Both repositories and both database backends require coordinated tests.
- The bot's public commands and chart/report behavior must remain available
  throughout the migration.
