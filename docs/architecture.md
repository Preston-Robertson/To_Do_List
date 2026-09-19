# Architecture

## Overview

Luigi Web is a FastAPI/Jinja2 application using server-rendered HTML, HTMX,
and locally hosted browser assets. It combines several intentionally separate
data domains behind one authenticated interface. It is a modular monolith:
purpose-built Python feature packages share a core host and one worker, not
separate services or fully independent task-domain repositories.

| Domain | Storage owner | Adapter |
|---|---|---|
| Tasks, recurring tasks, Discipline, follow-up rules | LuigiBot PostgreSQL schema | [../luigi_web/modules/tasks/repository.py](../luigi_web/modules/tasks/repository.py) |
| Recurring occurrence lineage and generation snapshots | Luigi Web-owned `luigi_web_recurring_occurrences` table in the same PostgreSQL database | [../luigi_web/modules/tasks/occurrences.py](../luigi_web/modules/tasks/occurrences.py) |
| Finance | Luigi Web SQLite database | [../luigi_web/modules/finance/repository.py](../luigi_web/modules/finance/repository.py) |
| Trading-card catalog, decks/versions, collection/purchase lots, and prices | One isolated Luigi Web SQLite database (Cards schema v4) | [../luigi_web/modules/cards/repository.py](../luigi_web/modules/cards/repository.py) |
| Games and shows: current fields | Game'N'Watch Google Sheet | [../luigi_web/modules/media/service.py](../luigi_web/modules/media/service.py) |
| Media web activity, replay/rewatch runs, and operation journal | Isolated Luigi Web SQLite database | [../luigi_web/modules/media/history.py](../luigi_web/modules/media/history.py) |
| Chat history | Process memory | [../luigi_web/modules/assistant/providers.py](../luigi_web/modules/assistant/providers.py) |
| Web-only task metadata fallback | Gitignored JSON | [../luigi_web/modules/tasks/repository.py](../luigi_web/modules/tasks/repository.py) |
| Feedback inbox | Luigi Web SQLite database | [../luigi_web/modules/feedback/repository.py](../luigi_web/modules/feedback/repository.py) |
| Sanitized maintainer queue | Shared app/worker SQLite database | [../luigi_web/modules/feedback/maintainer.py](../luigi_web/modules/feedback/maintainer.py) |
| Daily/Weekly Review sessions | Luigi Web SQLite database | [../luigi_web/modules/planning/repository.py](../luigi_web/modules/planning/repository.py) |
| Task dependencies, reminders, and date-scoped Home Today references | Luigi Web SQLite database | [../luigi_web/modules/tasks/operations.py](../luigi_web/modules/tasks/operations.py) |
| Tabletop characters and level states | Luigi Web SQLite database | [../luigi_web/modules/characters/repository.py](../luigi_web/modules/characters/repository.py) |
| Preview deployment | Root helper + isolated worktree/service/database | [../luigi_web/modules/preview/service.py](../luigi_web/modules/preview/service.py) |

Production code lives in [../luigi_web](../luigi_web).
[../app.py](../app.py) remains the compatibility entry point for existing
`uvicorn app:app` development commands and systemd deployments. Relocated
legacy modules use `sys.modules` identity aliases, not copied exports, so
existing imports and monkeypatches still refer to the same implementation.
In particular, [../luigi_web/db.py](../luigi_web/db.py) aliases the shared Tasks
repository used by both Tasks and Discipline. Disabling Tasks does not turn
Discipline into an independent database client.

## Trading Cards boundary

`LUIGI_WEB_CARDS_DB` remains the single Cards-owned SQLite database. Catalog,
decks, holdings, purchase lots, CSV preview state, deck versions, and price
history stay together for transactional writes and local joins. Cards schema
v4 adds `collection_lots` and `collection_imports`; version storage initializes
`deck_versions` in that same database. None of this changes LuigiBot's shared
tables/schema version, Finance storage/unlock, or existing environment settings.

[../luigi_web/modules/cards/purchases.py](../luigi_web/modules/cards/purchases.py)
owns the acquisition ledger. Migration creates one `legacy_summary` baseline per
existing holding, preserving partial priced quantities without inventing past
purchases. Actual entered costs, confirmed exact-date local market estimates,
legacy costs, and unknown copies stay distinct. Lots retain the original
printing across alternate-art swaps and survive holding removal.
[../luigi_web/modules/cards/collection.py](../luigi_web/modules/cards/collection.py)
owns filtered pagination, currency/coverage-aware totals, and bounded atomic CSV
preview/apply. Its active-lot export is not a removed-history/database backup.

[../luigi_web/modules/cards/analysis.py](../luigi_web/modules/cards/analysis.py)
reads a consistent, bounded snapshot for owned/needed plans, explicitly ordered
competing-deck allocation, board-aware statistics, and scoped non-blocking format
advisories. It neither reserves physical holdings nor performs online rules or
price lookups. Prices are dated cached estimates with unknown coverage retained.
Targeted price refresh is not implemented; the existing provider refresh paths
remain the source of current cache updates.

[../luigi_web/modules/cards/versions.py](../luigi_web/modules/cards/versions.py)
owns clone and same-deck version comparison/restore. Snapshots retain deck
metadata, printing slots, tags, and validated diagram XML, not collection or
catalog contents. Restore requires confirmed, hash-checked preview state, saves
an atomic pre-restore snapshot, and verifies the committed deck. Limits are
100 versions per deck and 2,000,000 bytes per snapshot; no room for the automatic
backup means no restore. These local snapshots are not full database backups.

[../luigi_web/modules/cards/composition.py](../luigi_web/modules/cards/composition.py)
is the manifest's router entry point and includes library, analysis, collection,
and version routes once. Existing URLs remain intact; the disposable Cards
preview mounts this same composition with synthetic records and no provider
access. All workflows retain authentication, browser CSRF, and private/no-store
responses. Collection/deck records stay out of LLM tools, global record search,
Finance, and external search. See [trading-cards.md](trading-cards.md) for user
workflows, routes, limits, and the offline synthetic performance measurement.

## Core and feature packages

[../luigi_web/application.py](../luigi_web/application.py) is the core host:
authentication/CSRF middleware, core endpoints, module mounting, lifecycle
coordination, and transitional helpers for already-existing modules. Feature
HTTP controllers now live at `luigi_web/modules/<id>/routes.py`; their
templates live under the same feature package. New external modules use core
APIs and request/app state, not application globals.

The core is deliberately small in responsibility:

- [../luigi_web/core/module_registry.py](../luigi_web/core/module_registry.py)
  defines the versioned manifest contract, built-in catalog, dependency
  checks, approved installed entry-point discovery, route validation,
  authentication/availability injection, and lifecycle ordering.
- [../luigi_web/core/module_settings.py](../luigi_web/core/module_settings.py)
  validates and atomically saves the next-restart selection, separately from
  feature records and credentials.
- [../luigi_web/core/modules_routes.py](../luigi_web/core/modules_routes.py)
  supplies the authenticated listing, filters, toggles, and save workflow.
  It does not install packages, change deployment approval, or restart the host.
- [../luigi_web/core/templating.py](../luigi_web/core/templating.py) supplies
  the shared shell context, namespaced external templates, and module assets.
- [../luigi_web/core/cli.py](../luigi_web/core/cli.py) implements `luigi-web`;
  its help path does not import the application.

All 11 built-ins remain enabled by default. Planning requires Tasks and
Discipline; Assistant requires Tasks, Discipline, and Media. Selection resolves
from an explicit registry argument, then `LUIGI_WEB_MODULES`, then the saved
file, then all built-ins. `/modules` saves changes for the next restart, with
environment-managed selections read-only. Disabling a module leaves data in
place and avoids its router imports and lifecycle hooks, but compatibility
imports still include the bundled shared adapter. Core-only and Cards-only
startup do not initialize PostgreSQL; Cards-only does not initialize Finance.

External modules are installed packages with approved `luigi_web.modules`
entry points, not arbitrary Git URLs installed through the GUI. Approval loads
trusted manifest code; enabling loads the router. Routes use
`/extensions/{id}` and cannot claim reserved or conflicting routes. Lifecycle
startup failures mark modules unavailable and block dependents without taking
down unrelated modules; invalid manifests and route construction can still
prevent host startup. There is no plugin sandbox: module code has the host's
process secrets and data privileges. Separate processes/containers with an
explicit HTTP contract are required for a real isolation boundary.

See [modules.md](modules.md) for the full selection, storage, packaging, and
external API contract, and
[../examples/example-module/README.md](../examples/example-module/README.md)
for the working independent package. The framework and example are shipped
in this tree; feature repositories have not all been split or published.

## Stack decision

Retain Python, FastAPI, Jinja, and HTMX. These workflows are primarily async
I/O and database interactions, and the existing stack has mature offline
regression tests. There is no performance evidence supporting a full language
or framework rewrite at this point. Adding a React/TypeScript build pipeline
now would add build and deployment cost without an established requirement.

Gradual TypeScript for a measured browser-maintenance need, or an independent
Rust/Go service for a measured workload, remains possible. Neither is a
prerequisite for modularity or a replacement already delivered by this
refactor. Keep the server-rendered contract until a specific need justifies
an additional boundary.

Before extracting an independently reusable task repository, move task Undo
and remaining shared host helpers behind stable core service contracts.
Preserve the LuigiBot shared schema and its ownership; this refactor does not
introduce schema migrations or claim complete domain independence.

## Shared LuigiBot schema

LuigiBot owns schema creation and `schema_version`. Luigi Web requires schema
version 2 and treats the following tables as shared contracts:

- `tasks`
- `recurring_tasks`
- `discipline_list`
- `discipline_completions`
- `follow_up_tasks`

The web app never inserts identity columns and scopes task-like mutations by
UUID. Optional web-owned task columns are added idempotently when permissions
allow; project/archive and recurrence metadata fall back to a gitignored app
file when they do not.

Recurring rows support an interval after completion, selected weekdays, or an
nth-weekday monthly position such as the first Monday. With explicit web
scheduler ownership, Luigi Web creates a fresh due successor on startup and
existing task/Home read paths, preserving the completed source row. The default
`LUIGI_WEB_RECURRENCE_OWNER=external` does no web scheduled generation, including
the former in-place reset. There is no background recurrence worker. Calendar
views project matching dates without persisting those projections. Completing a task through
the web UI or Assistant evaluates shared `follow_up_tasks` rules in the same transaction;
matching rules create one-off tasks, and the short Undo window removes tasks
generated by an accidental completion.

The optional shared `task_events` contract records immutable actual timestamps
and effective completion dates. LuigiBot owns its migration. Before that table
is installed, legacy completion remains available, Calendar marks completion
history unavailable, and explicit completion-date overrides are rejected.
Calendar nevertheless shows currently completed legacy rows, converting naive
legacy timestamps as UTC into the configured `LUIGI_WEB_TIMEZONE`; this is
explicitly labeled limited history until the ledger is available.

Calendar presentation state (layers, Month/Agenda mode, and density) is stored
in browser-local preferences only. The Activity view reads generic shared
events when available and falls back to current timestamp columns with a
visible limited-history warning before migration.

The current Discipline schema links completions by task text. A stable habit
definition in `discipline_list` has dated completion rows in
`discipline_completions`; completing a habit does not create a fresh task-like
copy or replace its definition. The coordinated UUID-keyed completion migration
in [discipline-v2-plan.md](discipline-v2-plan.md) remains future work with
LuigiBot, not an implemented storage contract.

### Discipline progress and lifecycle

[../luigi_web/modules/discipline/routes.py](../luigi_web/modules/discipline/routes.py)
keeps a full 365/366-day heatmap per habit and a year picker as the main view.
Annual history uses exact task-text lookup, falling back to case/outer-whitespace
normalization when no exact days are found.
[../luigi_web/modules/discipline/service.py](../luigi_web/modules/discipline/service.py)
merges completion dates by normalized title, also collapsing internal whitespace,
and delegates weekly math to
[../luigi_web/modules/discipline/progress.py](../luigi_web/modules/discipline/progress.py).
This is compatibility over the shared legacy adapter, not a UUID migration.

Weekly progress counts distinct completed dates from Monday through local today
in the current Monday-Sunday week, using `LUIGI_WEB_TIMEZONE`. Future dates do
not count. The current target applies only to that current-week display,
independently of the history year; no historical target versions are inferred.
The bar is capped at the target while text preserves excess completions. Only
seven-day targets show the current all-history daily streak on Discipline.
Non-daily targets have no daily-failure warning there; Review's existing
`list_disciplines_at_risk()` heuristic is unchanged.

Authenticated `GET /discipline/progress` returns `Cache-Control: no-store` JSON
with dates and UUID-associated statistics/state, not habit names. Verified
completion updates emit `luigi:discipline-updated`; the module's browser
controller reloads progress and retains confirmed values with a notice if that
refresh fails. Initial weekly-progress failure leaves available annual history
visible and progress unavailable, not a fabricated zero.

Pause and resume POSTs update only `discipline_list.active`, verify readback
inside the transaction, and return success only after commit. They retain
history; paused habits reject today's completion changes with `409` but allow
past-date corrections. Delete still removes the definition and completions;
its 12-second Undo depends on the Tasks module. Browser mutations retain the
shared authentication and same-origin cookie CSRF contract.

Search/category/status/target filters are page-memory-only. Pin/order metadata
uses browser-local `luigi.discipline.layout`, version 1, with at most 500 unique
habit IDs per array and no habit record content. Pinned and unpinned groups stay
separate; moves use visible peers in the same group. Writes are read back before
claiming persistence; failed storage leaves page-only changes and an error
notice. There is no cross-device synchronization.

Live per-habit **History** links use
[../luigi_web/modules/discipline/history.py](../luigi_web/modules/discipline/history.py)
for authenticated, no-store Month/Year/Log detail without replacing the main
annual heatmap. GET `/discipline/{uuid}/history` and `/history/data` read bounded
years through the shared repository's `list_discipline_history`; records retain
only `completed_date` and `logged_at`, not an actual completion-time input or
historical target versions. `change_discipline_history` checks a SHA-256 raw-row
version, verifies writes, and rejects ambiguous task-text identity. POST history
corrections and `/history/undo` require normal cookie CSRF and same-origin checks.
A bounded process-local 12-second Undo restores exact original rows/timestamps
only while the saved version matches. Paused today is blocked; past corrections
remain available. Failures require reloading confirmed state before another
write. Records are not persisted in browser storage, and no Finance, global
search, or Assistant integration is added.

The disposable workspace preview patches these history repository calls with
bounded adapters over the same in-memory rows used by Today, heatmap toggles,
and progress. Only exact seeded UUID history mutation paths are permitted;
the shared engine remains blocked. Cold-process tests cover default and combined
demo flags separately from the unchanged 16-endpoint `--check` smoke test.

Authenticated `/discipline/history-preview` still serves fixed synthetic Month/Year/Log
data and browser-memory-only corrections through the sample current date, with
future dates blocked. It does not read live habit history or write backend
records, and does not replace the live annual heatmap. See
[discipline.md](discipline.md) for the approved scope, controls, isolated preview,
and checks. Reminders, preferred weekdays, and successful-week streaks are not
implemented for Discipline.

### Recurring occurrence ledger

The app-owned PostgreSQL table `luigi_web_recurring_occurrences` records a
primary-key `parent_uuid`, unique `child_uuid`, `series_uuid`, `completed_at`,
`due_date`, `generated_at`, and nullable `metadata_json`. That JSON carries
optional child metadata and a completed-parent snapshot. It is separate from
LuigiBot's shared-table ownership and optional `task_events` contract. Creating
this ledger does not bump LuigiBot's schema version, alter a shared table, or
perform destructive DDL.

The ledger insert and new `recurring_tasks` child are verified and committed in
one transaction on the same PostgreSQL connection. This is not an Operations or
Finance SQLite sidecar, and it does not depend on cross-database commits.
PostgreSQL source-row locks, deterministic child UUIDs, and ledger uniqueness
prevent multiple successors per parent. Deleting a child leaves its consumed
ledger entry; changing ownership does not delete lineage or remove web history
guards. The rule lives on the current occurrence, not in a new template table.

`web` requires an explicit deployment handoff after disabling/coordinating the
legacy LuigiBot reset scheduler; the application neither detects nor disables
that process. This is not an Admin allow-listed setting or GUI toggle. Existing
web-only installations must now opt in. See
[recurring-occurrences.md](recurring-occurrences.md) for exact date semantics,
least-privilege storage requirements, adoption, correction, and recovery limits.

## Review and task automation

Daily/Weekly Review persists only checklist state and user notes in the
app-owned `LUIGI_WEB_REVIEW_DB`. Its work panels reuse current task and
Discipline queries, use `LUIGI_WEB_TIMEZONE`, and return no-store responses.
Weekly review covers the seven complete days ending yesterday.

Dependencies, reminder rules, generated notification state, and Home Today
references live in the existing app-owned `LUIGI_WEB_OPERATIONS_DB`.
Dependency edges are cycle-checked and enforced by the central Luigi Web
task status/completion methods, including edit, drag, bulk, and Assistant
paths. Deleting a task removes its local rules and Today references; the
short Undo snapshot restores them with the task, retaining selection dates.
This enforcement is web-only until LuigiBot adopts the same contract.

The operations Today API, `list_today_selections()` and
`set_today_selection()`, uses the added `home_today_selections` table. Its
only fields are `task_source`, `task_uuid`, and `selected_date`, with a
composite primary key; it holds references, not task payloads. The day is
`clock.local_today()` in the configured timezone. Membership persists across
restarts for that day, starts empty by default, and never silently carries
forward to the next day. Due dates do not automatically select tasks.
This app-owned addition requires no LuigiBot migration, schema-version bump,
or destructive shared-table change.

Automatic overdue, due-soon, and blocked notifications are reconciled with
current task state. Custom rules and larger due-soon windows use deterministic
fingerprints for deduplication. Dismiss and snooze remain local, and evaluation
is driven by authenticated inbox/count refreshes rather than a background or
external delivery service.

## Shared task backup and restore

The Admin JSON backup covers the five shared LuigiBot tables plus web-owned
task metadata. Restore validates the entire bounded payload before database
access, previews insert/update counts, consumes a one-time expiring token, and
performs UUID-based merge upserts in one shared-database transaction. Missing
backup rows never delete live rows. Restricted deployments that lack optional
web columns retain project, archive, and recurrence values in the metadata
fallback.

The current backup format excludes `luigi_web_recurring_occurrences`; shared-task
export/merge restore is not complete recurrence disaster recovery. Back up and
recover `recurring_tasks` and the occurrence ledger together from a consistent
PostgreSQL snapshot, along with applicable completion-event history and metadata
fallbacks. Do not merge old recurring rows without their matching ledger or
assume that merge-only restore enforces the occurrence edit/Undo guards. Never
delete ledger entries to force a retry: that can duplicate occurrences.

This shared-task artifact intentionally excludes Finance, Feedback, Review
notes, dependencies, reminders, credentials, and deployment settings. Those
stores retain their separate security and ownership boundaries.

## Finance boundary

Finance is app-owned and deliberately separate from LuigiBot. Its SQLite file
is configured by `LUIGI_WEB_FINANCE_DB` and initialized by `finance.init_db()`.

Design constraints:

- integer minor units for persisted money;
- one configured ISO base currency until exchange-rate support is added;
- user-selected display aliases only;
- no account/routing/card/tax identifiers;
- no LLM tools or global-search indexing;
- separate finance unlock token;
- in-memory CSV preview with duplicate detection and one-time commit tokens;
- immutable audit events for finance mutations;
- validated merge-style JSON backup and restore;
- budget, recurring-item, and stale-holding notifications;
- all Finance HTML, redirects, exports, and backups use no-store headers.

Finance tables:

- `finance_accounts`
- `finance_transactions`
- `finance_budgets`
- `finance_holdings`
- `finance_recurring_items`
- `finance_net_worth_snapshots`
- `finance_saved_reports`
- `finance_audit_events`

## Character sheets boundary

Tabletop characters are app-owned and stored in the isolated SQLite database
configured by `LUIGI_WEB_RPG_DB`. Character records, level states, and sheet
entries never share LuigiBot, Finance, or Trading Cards tables.

Each character has one current level state and can retain multiple independent
states for its build path. Cloning a state transactionally copies its sheet
entries so later hit-point, spell, resource, inventory, or note changes do not
rewrite earlier levels. Reusable `library_entries` remain separate from
state-owned `sheet_entries`; adding from the library creates an independent
copy with provenance. `class_progressions` maps library entries to class,
subclass, level, automatic-grant, and exact-count choice rules. Guided Level Up
validates the complete selection before cloning and applying all grants in one
SQLite transaction. Replacement-family keys remove obsolete imported feature
versions only from the new snapshot.

[../luigi_web/modules/characters/srd.py](../luigi_web/modules/characters/srd.py)
owns the optional 2014 SRD provider, with the legacy import retained as an
identity alias. Refresh is an
explicit authenticated POST, never startup work. It fetches only an allow-listed
set of fixed 5e-bits bulk URLs with redirects disabled, byte and row limits,
and local shape validation. Raw downloads are not persisted. Provider records
are reconciled transactionally by stable external key; stale provider records
are archived and provider-owned progression mappings are rebuilt without
changing manual library entries. Stored source/license metadata identifies SRD
5.1 and CC BY 4.0. No D&D Beyond or 5e.tools scraping is used.

Character and library data is excluded from Assistant tools, telemetry, and
global record search. All character responses use `Cache-Control: no-store`.

## Authentication and request security

The main UI uses a shared token stored in an HttpOnly cookie. Finance requires
a second token and cookie. Browser mutations include a same-origin CSRF token;
Bearer API clients authenticate without cookie CSRF.

Set `LUIGI_WEB_SECURE_COOKIES=1` behind HTTPS. Finance should not contain real
data on plain HTTP.

## Frontend

- [../luigi_web/core/templates/base.html](../luigi_web/core/templates/base.html)
  owns the module-aware responsive sidebar, command palette, drawers, and
  toast surfaces. It is one of four shared HTML templates; domain pages and
  partials live under their feature packages.
- [../luigi_web/core/static/css/app.css](../luigi_web/core/static/css/app.css)
  retains the feature visual system;
  [../luigi_web/core/static/css/shell.css](../luigi_web/core/static/css/shell.css)
  supplies the shared shell, responsive layouts, local IBM Plex Sans fonts,
  and reduced-motion styles. Lucide SVG icons are served locally.
- [../luigi_web/core/static/js/appearance.js](../luigi_web/core/static/js/appearance.js)
  restores the remembered Light, Dark, or System preference; System follows
  the browser's color-scheme preference.
- [../luigi_web/core/static/js/shell.js](../luigi_web/core/static/js/shell.js)
  enhances navigation and module selection;
  [../luigi_web/core/static/js/app.js](../luigi_web/core/static/js/app.js)
  retains HTMX events, commands, and legacy local preferences, deferring Tasks
  view state and column order to the module's view controller.
- HTMX and SortableJS are served from
  [../luigi_web/core/static/js/vendor](../luigi_web/core/static/js/vendor);
  fonts, icons, and browser libraries need no third-party CDN.
- Media Insights loads the vendored Chart.js build only on its page and keeps
  accessible data tables as the underlying readable representation.

Shared resource relocation does not change public `/static/...` URLs. External
modules use namespaced templates from `create_templates(package=..., namespace=...)`
and packaged static files under `/module-assets/{id}/...`. Static resources
must never contain private records or credentials.

### Tasks workspace

Tasks keeps the existing Board default and status order. Its module-owned view
controller stores validated filters, sorting, List grouping, Board/List mode,
density, and visible/collapsed columns in browser-local
`luigi.tasks.views.v1.<scope>` stores. Legacy saved filters and remembered mode
seed new endpoint-scoped stores without deleting old keys; these settings are
not account-specific or synchronized across devices. Source-qualified keys
keep one-off and recurring records distinct. Date filters use the server's
`data-task-calendar-date` page-load anchor, refreshed only on reload.

The shared task form presents optional Repeat during creation. `POST /tasks`
preserves repeated weekday values, chooses the existing one-off or recurring
repository method, and verifies the created row before success. Existing rows
are not converted between shared tables; `/recurring/new` remains compatible.
Editor assets load in the shared shell when Tasks is enabled, including Home's
existing modals, and failed saves retain the draft. No new shared schema or
framework is required.

The authenticated `/tasks/preview` serves fictional fixtures with memory-only
interactions and no SQL, mutating requests, or browser record storage. The
List-first/mobile proposal (#1) and Automation consolidation (#6) remain
examples awaiting approval, not replacements for the live Board or separate
advanced links. Simplified creation (#3), saved views (#4), and stable Board
controls (#5) are live; Tasks Today-membership actions (#2) are excluded.
See [tasks.md](tasks.md) for storage limits, compatibility, and validation.

### Home and on-demand Assistant

The shared shell mounts Assistant assets and a dialog only when that module
is enabled. Its authenticated `/chat/panel` endpoint initializes the provider
on demand and restores visible session messages with `Cache-Control: no-store`.
Opening the panel does not send a chat turn or pass the current page contents
to the provider. Existing POST chat/reset routes retain authentication and
cookie CSRF protection. Chat history remains process-local.

The approved Home redesign is live in Planning. Its
[../luigi_web/modules/planning/home_routes.py](../luigi_web/modules/planning/home_routes.py)
controller and
[../luigi_web/modules/planning/home_service.py](../luigi_web/modules/planning/home_service.py)
projection feed
[../luigi_web/modules/planning/templates/home.html](../luigi_web/modules/planning/templates/home.html).
Home still requires Tasks and Discipline through Planning; it is not a new
module-independent host. The projection includes all nonarchived task and
recurring-task rows without a 25-row limit. One task list offers Today,
Upcoming, and All, with Show completed in All. Habits, Today's progress,
Coming up, and static Continue links replace the duplicate summary tiles.

Authenticated `GET /home/data` returns no-store state. Authenticated
`POST /home/today` and `POST /home/reschedule` retain browser CSRF protection
and verify writes by readback. Stale-day Today submissions return `409` and
require a refreshed day before retrying. Task creation, editing, completion,
and habit actions reuse existing routes; date-only rescheduling uses the
existing Undo mechanism. Due dates and Today membership are independent.
Completing a selected task leaves it selected so it still counts in Today's
progress. Selection-storage failure is explicit and disables selection
without hiding all tasks; task-storage failure blocks Home with a generic
`503`, not database details.

Coming up uses actual due dates, not newly scheduled Calendar events or focus
blocks. Continue is module-aware static navigation, not a record query. Home
does not fetch Finance, card, character, media-provider, or Assistant-provider
records to fill these sections.

Planning owns browser-local Home layout preferences and an opt-in shared copy
at `DATA_DIR/home-layout.json`. Authenticated GET/PUT `/home/layout` accepts
only a bounded versioned list of known widget IDs, order, visibility, and pins.
It has no startup I/O and stores no records. Writes are atomic and verified;
failed commits do not produce a saved UI state. Main and supporting lanes
are fixed; moving and pinning operate within a lane. Five visible sections
reuse existing IDs, while all 13 previous IDs remain accepted and preserved
without rendering retired widgets. Layout order remains stable instead of
moving empty widgets after HTMX changes. Scope changes and saves use native
async cleanup for busy-state races. This is still one opt-in shared layout
for the single-user application. See [home.md](home.md) for the ID mapping
and **Customize layout > Restore defaults > Save** reset workflow.

The separate authenticated `/home/preview` route renders only synthetic
fixtures. Its task actions run in browser memory, with no record APIs or
mutating requests. It remains a comparison demo; the approved single task
list, supporting column, and compact empty states are now live on `/home`.

The disposable
[../scripts/preview_workspace.py](../scripts/preview_workspace.py) helper
instead exercises actual routes with bounded synthetic task adapters. Its
security hook defaults to task writes disabled; the helper explicitly opts
in after installing the adapters. Synthetic completion/reopen, create/edit,
date, Today, habit, and Undo actions operate on in-memory task/habit records
and a temporary operations Today store. Two preselected tasks are a
screenshot/demo seed only, not production defaults. Real shared
`get_engine` access stays blocked, as do integration, Admin, deployment,
external refresh, and chat writes. Existing disposable module/layout and
Cards/Characters actions remain available. Auto-login and the port-scoped
preview cookie do not change production login or sessions.

The same helper also seeds a recurring example and supports one-off quick
capture/status and recurring create/edit/status/completion/Undo through bounded
synthetic adapters. Exact synthetic record lookup, the route allow-list, and
browser CSRF checks still gate writes. Its `--check` covers 14 workspace
endpoints, including `/tasks/preview`, without exercising production writes.

## Local feedback

Feedback uses a separate app-owned SQLite database configured by
`LUIGI_WEB_FEEDBACK_DB`. Capture stores user-entered category/message and an
optional local path without query strings. It never captures form fields,
tokens, Finance data, chat history, or environment values. JSON/Markdown
downloads require explicit review and use `Cache-Control: no-store`.

An explicit **Queue for maintainer** action copies only privacy-screened request
fields and acceptance criteria into `LUIGI_WEB_MAINTAINER_DB`. The daily worker
can read that queue but not the raw Feedback database. Its repository clone,
Copilot runtime, and publishing credentials live in a private worker directory
that the web service cannot access. The worker produces draft pull requests;
it has no merge or deployment operation. See
[`autonomous-maintainer.md`](autonomous-maintainer.md).

## Preview boundary

Preview uses a separate Git worktree, Python runtime, systemd service,
credentials file, writable data directory, and PostgreSQL snapshot. Main UI
authentication can view metadata only. Mutations require a separate deployment
unlock and a root-owned helper whose verbs, refs, paths, service, and database
targets are fixed. Production Finance is never copied.

## Media boundary

The Games and Shows libraries keep current records in the shared Game'N'Watch
Sheet. [../luigi_web/modules/media/service.py](../luigi_web/modules/media/service.py)
owns header-addressed access, the 20-second section cache with single-flight
reads, version checks, batch cell updates, and readback verification.
[../luigi_web/modules/media/workspace.py](../luigi_web/modules/media/workspace.py)
coordinates authenticated library JSON routes, a process-local mutation lock,
12-second Undo, and 24-hour recent-pick exclusion. Browser saved views contain
preferences scoped by section/profile, not item records.

[../luigi_web/modules/media/history.py](../luigi_web/modules/media/history.py)
owns a separate `LUIGI_WEB_MEDIA_DB`, defaulting to `DATA_DIR/media.sqlite3`.
Initialization requires a new/empty database or an existing Media-owned file;
the SQLite `application_id` rejects other owners and nonempty unowned stores.
It never writes bot History/Sessions worksheets or changes LuigiBot's schema.
The shared Sheet's original start/completion dates remain first-ever dates;
replay/rewatch timestamps and retained run states belong to this local store.
History uses normalized section/profile/title identity and cannot yet follow
renames. Missing bot/external activity is not reconstructed.

The local journal reserves a before-state and expected fields before a Sheet
mutation, then confirms events/runs only after verified readback. **Sheet and
SQLite writes are not one atomic transaction.** Fresh-read reconciliation can
confirm matching intended fields, mark unchanged attempts unconfirmed, or
retain partial/ambiguous results without inventing an event. Local locking and
version checks do not provide distributed optimistic concurrency or atomic
compare-and-swap against external Google Sheets writers. Undo restores captured
cells and local run state only if newer state has not intervened, and shares
these cross-store limitations. Failed confirmation stays visible; the browser
updates confirmed content only and requires explicit recovery/retry.

[../luigi_web/modules/media/steam.py](../luigi_web/modules/media/steam.py)
separates cached GET display from explicit POST refresh and playtime save.
Snapshots bind profile/title/app ID and configuration identity, become stale
at 300 seconds, and are save-eligible only before 600 seconds. Missing/private
data stays unknown, and achievements never automatically change status.
Insights use confirmed web activity, separating legacy baselines from timed
statistics; they do not derive velocity from lifetime counters. Responses use
generic errors and no-store JSON, with the host's authentication/CSRF boundary.
See [media.md](media.md) for all seven workflows, routes, recovery semantics,
and synthetic validation commands.

## Integrations

Game'N'Watch uses a service account and header-addressed Google Sheet access.
Catalog metadata comes from Steam, TVMaze, AniList, and optional YouTube.

The optional Assistant uses either the official GitHub Copilot SDK or an
OpenAI-compatible endpoint. The Copilot SDK runs in `mode="empty"` with only
`custom:*` enabled; shell, filesystem, web, MCP, skills, and host instruction
discovery are disabled. Its allow-listed tools can access task and Discipline
helpers only. Finance is excluded by policy and code.

## Runtime

The provided systemd unit and installed CLI run one Uvicorn worker because Undo
snapshots, chat history, integration clients, and live configuration are
process-local. Module selection changes require a restart. All machine-specific
values come from an environment file.

[../pyproject.toml](../pyproject.toml) defines `luigi-web` 0.1.0 for Python 3.11+,
preserving the runtime dependencies from
[../requirements.txt](../requirements.txt). Wheels include core and feature
templates/assets. [../luigi_web/paths.py](../luigi_web/paths.py) anchors those
packaged resources separately from writable defaults: optional
`LUIGI_WEB_DATA_DIR`, the legacy source `data/` directory, or platform per-user
storage when installed. Existing per-feature storage overrides remain valid.

## Validation

```powershell
pip install "setuptools>=68" wheel
python -m unittest discover -s tests -p "test_home*.py" -v
python -m unittest discover -s tests -p "test_operations.py" -v
python -m unittest discover -s tests -v
python scripts/validate_repo.py
python scripts/preview_workspace.py --check
git diff --check
```

Use a clean development process without production credentials. The validator
compiles the shared template loader's HTML and checks mounted route
registrations without running startup hooks; route coverage depends on module
selection. Select all built-ins explicitly when checking the complete host.
The independent example's packaging tests additionally compile and serve its
namespaced template from installed wheels in disposable environments.

The workspace preview check visits 13 synthetic endpoints, including
`/home/data`, without exercising production integrations or storage writes.
Use the focused Home/operations tests and browser checks in [home.md](home.md)
to validate Today persistence, date independence, failure handling, layout
compatibility, and the bounded preview write paths.

Wheel tests require both `setuptools` and `wheel`; the wheel test class is
skipped if either is missing. Report skipped packaging checks rather than
claiming installed-wheel verification from an otherwise successful suite.
Frontend changes also require responsive browser review at 1440x900 and
390x844, including appearance persistence and reduced motion, using only
synthetic records.
