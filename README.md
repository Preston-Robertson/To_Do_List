# Luigi Web

A private, self-hosted command center for tasks, recurring work, habits,
projects, personal media, and finance. Luigi Web is a server-rendered FastAPI
application designed to run alongside
the shared LuigiBot repository.

The 0.2 host composes independently packaged features behind a shared Jinja/HTMX
shell. The 11 canonical feature roots live in [module-repos](module-repos), each
ready for its own GitHub repository. The combined checkout makes all 11 available
and selects them by default; an installed host alone has zero features. Smaller
selections can run without PostgreSQL. Existing data ownership, authentication,
and source deployment entry points are preserved. No remote repositories or
releases are created simply by checking out this project.

## Release Channels

- `main` is the stable production branch. Admin self-update fetches only
  `origin/main`, uses fast-forward-only updates, and retains the previous commit
  under a local `rollback/pre-update-*` branch before changing code.
- `testing` is the ongoing development and integration branch. Test changes
  there, then promote reviewed changes to `main`; production never follows
  `testing` automatically.
- This checkout is prepared for the 1.0 launch. The application release label
  is separate from the modular host/feature package compatibility version 0.2.
  No release tag is implied by a branch name or an update operation.

See [docs/releases.md](docs/releases.md) for promotion, first-update migration,
rollback, and the remaining deployment acceptance checks.

## Highlights

- **Tasks:** Board-first workspace with stable status columns, an optional
  compact List, browser-local saved views, name-only quick capture, and one
  Add task form with optional Repeat. Existing snooze, Undo, completion triggers,
  interval/weekday/monthly-position recurrence, projects, archive, dependencies,
  local reminders, a migration-aware Activity timeline, and projected recurring
  occurrences on Month/Agenda calendar views remain available. See
  [docs/tasks.md](docs/tasks.md) for live behavior and example-only proposals.
  Copy-per-occurrence scheduling preserves completed rows when explicitly
  enabled; it is off by default. See
  [docs/recurring-occurrences.md](docs/recurring-occurrences.md) for ownership,
  upgrade, and backup requirements.
- **Discipline:** full-year heatmaps remain the main view, with current-week
  target progress, daily streaks for seven-day targets, pause/resume, search,
  category filters, and browser-local pins/order. History is shared with LuigiBot.
  Detailed Month/Year/Log history is now live through each habit's **History**
  link, with version-checked corrections and 12-second Undo. The main heatmap
  and separate synthetic **History example** remain. See
  [docs/discipline.md](docs/discipline.md) for behavior, browser-local organization,
  and preview commands.
- **Projects:** named-project timeline with scheduled and unscheduled work.
- **Games and shows:** Continue-first library with Board/List views,
  browser-local saved filters, filtered weighted picks, quick show progress,
  explicit Steam refresh/playtime saves, and replay/rewatch history. Current
  records stay in the shared Game'N'Watch Google Sheet; confirmed web activity
  uses an isolated local store. Insights separate recent web-recorded changes
  from legacy completion baselines. See [docs/media.md](docs/media.md) for
  storage, confirmation limits, Undo, and synthetic validation.
- **Trading Cards:** local catalogs, full Table/40-row Stacks deck views,
  owned/needed build plans with explicit competing-deck allocation, board-aware
  statistics and scoped format advisories, plus clone/version comparison and
  guarded restore. Filtered, paginated collections track purchase lots with
  separate actual, estimated, legacy, and unknown costs, retained history,
  CSV exchange, and dated cached market values in one isolated app-owned
  database. See [docs/trading-cards.md](docs/trading-cards.md) for limits,
  pricing provenance, preview, and the measured local catalog benchmark.
- **Characters:** private D&D 5e (2014) and Pathfinder 2e sheets with live
  resources, abilities, saves, skills, spells, equipment, notes, and cloned
  level-path states, plus a reusable local rules library and guided class
  progression.
- **Finance:** separately unlocked overview with current balances, recorded
  monthly flows, net worth, budgets, upcoming bills, and a filtered ledger.
  Local cash-flow, wealth, and housing scenarios add take-home income streams
  and saved assumptions without changing actual balances or transactions.
  The records workspace retains accounts, investments, CSV import/export,
  reports, alerts, audit history, and backups. Finance stays isolated from
  LuigiBot and the LLM. See [docs/finance.md](docs/finance.md) for assumptions,
  limits, privacy, and synthetic preview commands.
- **Assistant:** optional GitHub Copilot subscription or OpenAI-compatible chat
  with an allow-listed task tool registry, opened on demand from the shared
  toolbar. Opening the panel does not send a message or capture page content.
  Finance is intentionally excluded.
- **Home:** the approved redesign is live at `/home`: one task list with Today,
  Upcoming, and All views, habits, Today's progress, Coming up, and static
  Continue links, without duplicate summary tiles. Today is an explicit daily
  selection, independent of due dates. Show, hide, pin, reorder within fixed
  lanes, and reset the layout; browser-local preferences remain the default,
  with sharing across devices opt-in. See [docs/home.md](docs/home.md).
- **Operations:** guided Daily/Weekly Review, verified preview-first shared-task
  restore, integration health, environment editor, self-update, local Feedback,
  bulk task actions, isolated branch
  Preview, responsive navigation, and `Ctrl+K` global commands.
- **Maintenance review (opt-in):** privacy-approved Feedback becomes two or three
  sandbox-tested options with synthetic screenshots. A human selects publication,
  reviews an isolated test application, and separately approves a versioned
  merge/tag after verified CI. Both review and release gates default off; see
  [docs/autonomous-maintainer.md](docs/autonomous-maintainer.md).
- **Modules:** authenticated catalog and restart-time selection, packaged
  templates/assets, and public GitHub release registration and queued installation
  constrained by deployment policy. A separate worker stages pinned wheels;
  nothing hot-loads or automatically restarts the app.

## Screenshots

Screenshot images remain local and are excluded from publication. Privacy-safe
capture guidance is documented in
[docs/screenshots/README.md](docs/screenshots/README.md); use synthetic data only.

## Privacy and security

Finance is designed around data minimization:

- no fields for legal names, account numbers, routing numbers, card numbers,
  tax identifiers, addresses, email addresses, or phone numbers;
- persisted money uses integer minor units and ISO currency codes;
- raw CSV uploads are parsed in memory and are not retained;
- Finance requires a separate unlock token in addition to the main session;
- Finance records are excluded from chat tools, global search, URLs, logs,
  screenshots, and public fixtures;
- Finance uses an app-owned SQLite database outside the repository;
- browser mutations use same-origin CSRF protection;
- secure cookies can be enforced behind HTTPS.

Read [`SECURITY.md`](SECURITY.md) before storing real financial data. HTTPS and
`LUIGI_WEB_SECURE_COOKIES=1` are strongly recommended.

## Character library and level paths

The authenticated Character Library stores reusable actions, features, spells,
equipment, resources, proficiencies, and conditions for either supported game
system. Adding a library entry to a sheet copies it into that level state, so
character-specific edits never rewrite the source record or an earlier state.
Library entries can also be mapped to class levels as automatic grants or
exact-count choices; guided Level Up clones the current state, applies those
rules transactionally, and keeps upgraded feature families from accumulating
obsolete versions.

D&D 5e (2014) can be populated with an explicit **Refresh open SRD** action.
The refresh downloads a fixed, bounded set of English 2014 bulk JSON files
from the [5e API data project](https://github.com/5e-bits/5e-database), validates
them locally, and retains only normalized library records. It does not run at
startup, retain raw payloads, or scrape D&D Beyond or 5e.tools. Manual records
are preserved when provider records are reconciled. Pathfinder 2e library
records are currently user-created.

This feature includes material from the D&D 5e System Reference Document 5.1
by Wizards of the Coast, available from the
[D&D SRD page](https://www.dndbeyond.com/srd), and licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

## Quick start

Requirements:

- Python 3.11+
- a LuigiBot PostgreSQL database at schema version 2 when Tasks or Discipline
  is enabled, including modules that depend on them
- optional Google Sheets credentials for Games/Shows

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-modules.txt
luigi-web --help
Copy-Item .env.example .env
```

Set the placeholder values in `.env` and load them into the process environment;
the CLI does not load this file automatically. For local loopback HTTP only,
disable secure-only cookies before running:

```powershell
$env:LUIGI_WEB_SECURE_COOKIES = "0"
python -m uvicorn app:app --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080` and sign in with `LUIGI_WEB_UI_TOKEN`. Finance
requires the separate `LUIGI_WEB_FINANCE_TOKEN`. Use
`LUIGI_WEB_SECURE_COOKIES=1` for HTTPS deployment.

The installed `luigi-web` command also runs the host, for example
`luigi-web --host 127.0.0.1 --port 8080`. Its `--help` path does not import the
application or initialize databases. Source deployments can keep using
[`app.py`](app.py) unchanged. See [docs/modules.md](docs/modules.md) for module
selection, wheel installation, and the independently installable example. The
editable requirements above install the host and all 11 feature distributions;
`pip install -e .` alone installs only the host. Fixed checkout namespace paths
make feature source importable, but do not install dependencies or metadata.

## Modules

The built-in IDs are `tasks`, `discipline`, `planning`, `media`, `cards`,
`characters`, `finance`, `assistant`, `admin`, `preview`, and `feedback`.
`planning` requires `tasks` and `discipline`; `assistant` also requires `media`.
Selection dependencies are enforced, not automatically added by the registry.

For a Cards and Characters instance without PostgreSQL, set the selection
before starting the host in the configured environment above:

```powershell
$env:LUIGI_WEB_MODULES = "cards,characters"
luigi-web --host 127.0.0.1 --port 8080
```

Use `none` for core-only startup without PostgreSQL. Disabling modules leaves
their data intact and skips their routers and lifecycle hooks. It does not
uninstall packages: Tasks and Discipline still share the Tasks package's
LuigiBot adapter when those features are used. Discipline requires the Tasks
Python distribution even with the Tasks UI off.

Selection priority is an explicit registry argument, then `LUIGI_WEB_MODULES`,
then the saved module-selection file, then all available reserved features. The authenticated
`/modules` page filters the catalog and saves validated selections atomically
for the **next restart**, without hot reload or deployment. When
`LUIGI_WEB_MODULES` is set, selection is deployment-managed and read-only there.

The core `/modules/repositories` page remains available with Admin disabled;
Admin also links to it. A deployment-approved owner list permits authenticated
registration with explicit code-trust confirmation, or a read-only policy file
supplies fixed repositories. Users queue an exact public GitHub release tag,
wheel filename, and SHA-256. A separate worker validates and stages the wheel
for the next restart, with dependencies already installed. It never clones Git,
builds source, runs pip, or restarts Uvicorn. Save an explicit `/modules` selection
before adding reserved features if they must not join the default at restart.

Separately installed external packages can still use the deployment-only
`LUIGI_WEB_EXTERNAL_MODULES` entry-point allow-list. Repository-staged external
packages derive approval from the repository policy and still require selection.
Neither policy is editable by Admin. Modules are trusted code with the host
process's secrets and data privileges, not sandboxed plugins.

See [docs/modules.md](docs/modules.md) for configuration and the versioned API,
and [docs/module-repositories.md](docs/module-repositories.md) for release
publication, GUI installation, and the optional one-time worker setup. The
[example package](examples/example-module/README.md) demonstrates the core API;
the 11 feature roots are independent source packages, not already published repos.

## Configuration

Copy [`.env.example`](.env.example) and keep the real file outside version
control. Core settings:

| Variable | Purpose |
|---|---|
| `LUIGI_WEB_MODULES` | Comma-separated IDs or `none`; overrides saved selection and makes GUI selection read-only |
| `LUIGI_WEB_MODULES_FILE` | Optional selection-file path; defaults to `DATA_DIR/modules.json` |
| `LUIGI_WEB_EXTERNAL_MODULES` | Deployment-only allow-list of installed `luigi_web.modules` entry-point names |
| `LUIGI_WEB_MODULE_REPOSITORY_OWNERS` | Deployment-only public GitHub owners allowed for GUI repository registration |
| `LUIGI_WEB_MODULE_REPOSITORIES_FILE` | Optional absolute path to a deployment-owned, read-only JSON repository policy |
| `LUIGI_WEB_DATA_DIR` | Optional writable defaults root; source `data/` or platform per-user storage when installed |
| `LUIGI_WEB_PG_*` | Shared LuigiBot PostgreSQL connection |
| `LUIGI_WEB_PG_CONNECT_TIMEOUT` | Connection timeout in seconds, clamped to `1`–`30` (default `5`) |
| `LUIGI_WEB_RECURRENCE_OWNER` | Deployment-managed `external` (default, no web scheduled generation) or explicit `web` after disabling/coordinating the legacy LuigiBot reset scheduler |
| `LUIGI_WEB_UI_TOKEN` | Main application login token |
| `LUIGI_WEB_FINANCE_TOKEN` | Separate Finance unlock token |
| `LUIGI_WEB_SECURE_COOKIES` | Set to `1` behind HTTPS |
| `LUIGI_WEB_TIMEZONE` | IANA timezone for user-facing dates (default `America/New_York`) |
| `LUIGI_WEB_DAY_CUTOFF` | Local `HH:MM` cutoff for previous-day completion (default `04:00`) |
| `LUIGI_WEB_FINANCE_DB` | App-owned Finance SQLite path |
| `LUIGI_WEB_FINANCE_BASE_CURRENCY` | ISO currency used for reports |
| `LUIGI_WEB_REVIEW_DB` | App-owned Daily/Weekly Review SQLite path |
| `LUIGI_WEB_OPERATIONS_DB` | App-owned task dependencies, reminders, and date-scoped Home Today references SQLite path |
| `LUIGI_WEB_RPG_DB` | App-owned character sheets and level paths SQLite path |
| `LUIGI_WEB_LLM_*` | Optional GitHub Copilot or OpenAI-compatible assistant |
| `LUIGI_WEB_COPILOT_HOME` | Writable cache for the bundled Copilot runtime |
| `LUIGI_WEB_GNW_*` | Optional Game'N'Watch Google Sheet |
| `LUIGI_WEB_STEAM_*` | Optional Steam progress integration |
| `LUIGI_WEB_YOUTUBE_API_KEY` | Optional playlist search |

**Recurrence upgrade compatibility:** installing this update does not
automatically activate copy-per-occurrence generation in production. The default
`LUIGI_WEB_RECURRENCE_OWNER=external` performs no web scheduled generation and
never runs the old web reset behavior. Existing web-only installations that
relied on automatic recurrence now need an explicit deployment opt-in to `web`,
only after disabling/coordinating LuigiBot's legacy reset scheduler. Luigi Web
does not detect or disable that scheduler. `external` leaves recurrence handling
external; it cannot preserve copy history if a legacy bot still resets rows.
Ownership is not an Admin setting or a GUI toggle. Read the
[recurrence guide](docs/recurring-occurrences.md) before changing it.

Until the shared `task_events` migration is installed, Calendar still displays
currently completed task rows using `completed_time`, converted from legacy UTC
timestamps into `LUIGI_WEB_TIMEZONE`. It labels this as limited history because
older recurring completions cannot be reconstructed after earlier in-place
resets. New copy-per-occurrence scheduling does not reconstruct that lost history.

The authenticated Admin page can edit allow-listed settings and run read-only
integration checks. It deliberately cannot read or change
`LUIGI_WEB_UI_TOKEN` or `LUIGI_WEB_FINANCE_TOKEN`: allowing the main session to
replace either credential would defeat the Finance security boundary.

Admin's shared-task backup uses a validated, merge-only restore: the upload is
fully parsed before database access, a preview lists inserts and updates, and a
one-time confirmation token gates the transactional commit. Rows absent from a
backup are never deleted. This backup covers the five LuigiBot task tables and
web-owned task metadata; it intentionally excludes Finance, Feedback, Review
notes, dependency/reminder rules, credentials, and deployment settings.
It also excludes the new `luigi_web_recurring_occurrences` ledger, so it is not
a complete recurrence disaster-recovery backup. Coordinate a consistent
PostgreSQL backup containing both `recurring_tasks` and that ledger; do not
restore old recurring rows without their matching ledger.
The task-only restore rejects rows or metadata for completed occurrences that
already have successors, rechecking that guard inside its transaction.

Task dependencies and reminders are local Luigi Web features. Every Luigi Web
status/completion path enforces blockers, but LuigiBot requires a coordinated
change before it can enforce the same rules. Reminder notifications are
generated and deduplicated when the in-app count or inbox refreshes; no task
content is sent to an external notification service.

For the systemd deployment, store authentication tokens in
`/etc/luigi-web/credentials.env`, owned by `root:root` with mode `0600`:

```text
LUIGI_WEB_UI_TOKEN=<long-random-ui-token>
LUIGI_WEB_FINANCE_TOKEN=<different-long-random-finance-token>
```

Generate each value independently with a password manager or with
`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. The service
example loads this protected file after the Admin-managed
`/opt/luigi-web/luigi.env`, so protected values take precedence. After moving
the current values, remove both token lines from the Admin-managed file, run
`systemctl daemon-reload`, and restart the service. Token rotation is
intentionally a host-administrator operation rather than a web action.

GitHub Models was retired on July 30, 2026. Use
`LUIGI_WEB_LLM_PROVIDER=copilot` to authenticate through the official GitHub
Copilot SDK and consume the configured account's Copilot allowance. The SDK is
run in empty mode and receives only Luigi Web's existing task tools. Local
interactive installs can use an existing GitHub CLI login; headless services
should set `LUIGI_WEB_LLM_API_KEY` to a supported fine-grained GitHub token.
Legacy configurations that still name the retired GitHub Models endpoint are
automatically routed through the Copilot SDK using their existing GitHub token.
If that configured token is rejected before any task tool runs, Luigi Web
retries once with the service account's existing Copilot login. Failures name
which authentication methods were unavailable without exposing credentials.

## Feedback and maintenance

Feedback stays in the local inbox until explicitly reviewed and exported or
approved for the separate maintainer queue. Only privacy-screened requests
enter that queue; raw Feedback and application records are not sent to the
coding worker. The local worker uses remote GitHub Copilot inference, not an
offline LLM. Candidate execution is secret-free and rootless; publication
requires human option selection. A separate release controller may merge and
tag only after explicit UI approval bound to verified CI, preview HEAD, and
version. It never deploys production. Email is notification-only. See
[docs/autonomous-maintainer.md](docs/autonomous-maintainer.md) for the trust
boundary, protected default-off gates, and unverified Linux deployment checks.

## Future features

- **Currency conversion:** explicit exchange rates and dated conversions for
  multi-currency reports. Until then, Finance accounts must use the configured
  base currency so totals remain mathematically valid.

## Deployment

A generic hardened systemd example is provided in
[`luigi-web.service`](luigi-web.service). It contains no machine-specific host,
address, or container identifiers; all deployment values come from a protected
environment file.

Keep private deployment notes in `LOCAL_DEPLOYMENT.md`, which is gitignored.
Serve the app behind a TLS reverse proxy before adding real finance data.

## Development

Run the offline regression suite and repository checks from the repository root:

```powershell
pip install "setuptools>=68" wheel
python -m unittest discover -s tests -p "test_home*.py" -v
python -m unittest discover -s tests -p "test_operations.py" -v
python -m unittest discover -s tests -p "test_task*.py" -v
python -m unittest discover -s tests -p "test_discipline*.py" -v
python -m unittest discover -s tests -v
python scripts/validate_repo.py
git diff --check
```

Application code lives in [luigi_web](luigi_web), with core resources under
[luigi_web/core](luigi_web/core) and feature routes/templates under
[module-repos](module-repos), in each root's `src/luigi_web/modules/<id>` package.
The host retains the namespace bridge and legacy import aliases, not duplicate
feature implementations. Root [app.py](app.py) intentionally
remains the stable `uvicorn app:app` compatibility entry point used by source
development and the supplied systemd service. The installed CLI also runs one
worker because Undo, chat history, and live integration state remain
process-local.

The tests use synthetic data only and do not require live PostgreSQL, Google,
Steam, bank, or LLM credentials. Use a clean development environment, not
production credentials. Packaging tests build and install the host and example
in disposable directories; their wheel test class is skipped if `setuptools`
or `wheel` is missing. Check the reported skips before claiming wheel coverage.
The repository validator compiles shared-loader templates and checks mounted
routes without running startup hooks. Route coverage follows module selection;
select all built-ins explicitly for a complete host check. External template
namespaces are covered separately by the example's packaging tests.

Frontend changes also need browser review at 1440x900 and 390x844. The shell
uses local IBM Plex Sans fonts and Lucide icons, remembers Light/Dark/System
appearance, and includes reduced-motion and responsive styles; no CDN or
React/TypeScript build is required. Module boundaries and supported extension
APIs are documented in [docs/modules.md](docs/modules.md).

For a disposable local UI preview, run:

```powershell
python scripts/preview_workspace.py --check
python scripts/preview_workspace.py
```

The helper prints an available loopback URL, uses synthetic records and
temporary databases, and blocks deployment actions and external refreshes.
This disposable preview signs in automatically, including direct links; do
not enter production credentials. Preview sessions are scoped by port and do
not replace the normal application's session. Production login is unchanged.
The helper opts into bounded synthetic task adapters for completion/reopening,
creation, editing, dates, Today membership, habits, and Undo. Task and habit
records stay in memory; Today references use a temporary operations database.
It also seeds an example recurring task and supports one-off quick capture and
status changes, plus recurring creation, editing, status, completion, and Undo.
Task mutations require a valid demo session, allow-listed synthetic IDs/routes,
and the normal browser CSRF checks.
The preview preselects two tasks for screenshot/demo coverage only; production
starts with no Today selections. Real shared `get_engine` access, integration,
Admin, external refresh, deployment, and chat writes remain blocked.

Module selection, Home layout preferences, and Cards/Characters changes stay
in disposable storage. Assistant opens in its unconfigured state without
contacting a provider. The helper does not exercise production integrations
or apply saved module selections to a running process. Stop it with Ctrl+C
when done. Its `--check` command checks 16 synthetic workspace endpoints,
including `/home/data`, `/tasks/preview`, `/discipline/progress`, and
`/discipline/history-preview`; it is not a production write test.

The default preview still has one habit. Add `--discipline-demo` for four
synthetic habits with multiple categories, weekly/daily targets, and one paused
habit. The flag can be combined with `--occurrence-demo`:

```powershell
python scripts/preview_workspace.py --discipline-demo --check
python scripts/preview_workspace.py --discipline-demo
python scripts/preview_workspace.py --discipline-demo --occurrence-demo
```

Discipline preview writes are limited to synthetic pause/resume, heatmap
toggles, today's completion, and version-checked live history corrections/Undo
for seeded habit UUIDs. These share one in-memory completion state and retain
original logged timestamps on Undo; habit creation, editing, and deletion
remain blocked. **History example** in the toolbar still opens the separate,
memory-only example. The 16-endpoint smoke check is unchanged; dedicated cold
subprocess tests cover the live history paths. See
[docs/discipline.md](docs/discipline.md) for the legacy date/logged-at limits,
live behavior, example boundary, and validation commands.

**Tasks > Examples** opens `/tasks/preview`: proposal #1 (List-first workspace
with compact mobile rows) and proposal #6 (consolidated Automation tabs) remain
examples only, awaiting approval. Their fictional records and simulated changes
stay in browser memory and reset on reload; they do not replace the live Board
default or the separate completion-trigger, task-rule, and archive links.

The approved Home layout is now live at `/home`. The separate authenticated
`/home/preview` remains a synthetic comparison page: its simulated changes
are browser-memory-only and reset on reload or **Reset demo**. See
[docs/home.md](docs/home.md) for Today storage, layout compatibility, and
focused validation.

Repository guidance:

- [docs/modules.md](docs/modules.md) - selection, packaging, and trusted
  external-module contracts
- [examples/example-module/README.md](examples/example-module/README.md) -
  editable-install demo and independent wheel verification
- [docs/discipline.md](docs/discipline.md) - live annual heatmaps, weekly targets,
  Month/Year/Log history, pause/resume, browser-local organization, and separate
  synthetic history example
- [docs/tasks.md](docs/tasks.md) - task workflows and completion history
- [docs/recurring-occurrences.md](docs/recurring-occurrences.md) - recurring
  task instances and history
- [`docs/preview-deployment.md`](docs/preview-deployment.md) — isolated Git
  branch Preview worktree/service setup
- [docs/autonomous-maintainer.md](docs/autonomous-maintainer.md) - Phase 2
  human-approved options, isolated testing, release gates, and role deployment
- [docs/maintainer-test-preview.md](docs/maintainer-test-preview.md) - separate
  HTTPS test application, ticket/session boundary, and preview lifecycle
- [`docs/screenshots/README.md`](docs/screenshots/README.md) — privacy-safe local screenshot
  capture checklist
- [`SECURITY.md`](SECURITY.md) — security and sensitive-data policy

Agent instructions, internal architecture notes, future coordination plans,
and dated development reports remain developer-local, outside the published
documentation.

## License

No license has been declared for Luigi Web itself. Runtime-imported SRD 5.1
material retains its CC BY 4.0 attribution described above. Add a project
license before accepting external contributions.
