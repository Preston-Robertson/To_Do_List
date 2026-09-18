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
| Finance | Luigi Web SQLite database | [../luigi_web/modules/finance/repository.py](../luigi_web/modules/finance/repository.py) |
| Trading-card catalog, decks, collection, and prices | One isolated Luigi Web SQLite database | [../luigi_web/modules/cards/repository.py](../luigi_web/modules/cards/repository.py) |
| Games and shows | Game'N'Watch Google Sheet | [../luigi_web/modules/media/service.py](../luigi_web/modules/media/service.py) |
| Chat history | Process memory | [../luigi_web/modules/assistant/providers.py](../luigi_web/modules/assistant/providers.py) |
| Web-only task metadata fallback | Gitignored JSON | [../luigi_web/modules/tasks/repository.py](../luigi_web/modules/tasks/repository.py) |
| Feedback inbox | Luigi Web SQLite database | [../luigi_web/modules/feedback/repository.py](../luigi_web/modules/feedback/repository.py) |
| Sanitized maintainer queue | Shared app/worker SQLite database | [../luigi_web/modules/feedback/maintainer.py](../luigi_web/modules/feedback/maintainer.py) |
| Daily/Weekly Review sessions | Luigi Web SQLite database | [../luigi_web/modules/planning/repository.py](../luigi_web/modules/planning/repository.py) |
| Task dependencies and reminders | Luigi Web SQLite database | [../luigi_web/modules/tasks/operations.py](../luigi_web/modules/tasks/operations.py) |
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
nth-weekday monthly position such as the first Monday. Luigi Web reactivates
due rows on startup and task-page reads. Calendar views project matching
occurrences without inserting duplicate task rows. Completing a task through
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

The current Discipline schema links completions by task text. The coordinated
UUID migration plan is in `discipline-v2-plan.md` and must be delivered with
LuigiBot.

## Review and task automation

Daily/Weekly Review persists only checklist state and user notes in the
app-owned `LUIGI_WEB_REVIEW_DB`. Its work panels reuse current task and
Discipline queries, use `LUIGI_WEB_TIMEZONE`, and return no-store responses.
Weekly review covers the seven complete days ending yesterday.

Dependencies, reminder rules, and generated notification state live in
`LUIGI_WEB_OPERATIONS_DB`. Dependency edges are cycle-checked and enforced by
the central Luigi Web task status/completion methods, including edit, drag,
bulk, and Assistant paths. Deleting a task removes its local rules; the short
Undo snapshot restores them with the task. This enforcement is web-only until
LuigiBot adopts the same contract.

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
  retains HTMX events, Board/List switching, commands, and local preferences.
- HTMX and SortableJS are served from
  [../luigi_web/core/static/js/vendor](../luigi_web/core/static/js/vendor);
  fonts, icons, and browser libraries need no third-party CDN.
- Media Insights loads the vendored Chart.js build only on its page and keeps
  accessible data tables as the underlying readable representation.

Shared resource relocation does not change public `/static/...` URLs. External
modules use namespaced templates from `create_templates(package=..., namespace=...)`
and packaged static files under `/module-assets/{id}/...`. Static resources
must never contain private records or credentials.

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
python -m unittest discover -s tests -v
python scripts/validate_repo.py
git diff --check
```

Use a clean development process without production credentials. The validator
compiles the shared template loader's HTML and checks mounted route
registrations without running startup hooks; route coverage depends on module
selection. Select all built-ins explicitly when checking the complete host.
The independent example's packaging tests additionally compile and serve its
namespaced template from installed wheels in disposable environments.

Wheel tests require both `setuptools` and `wheel`; the wheel test class is
skipped if either is missing. Report skipped packaging checks rather than
claiming installed-wheel verification from an otherwise successful suite.
Frontend changes also require responsive browser review at 1440x900 and
390x844, including appearance persistence and reduced motion, using only
synthetic records.
