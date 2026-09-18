# Luigi Web

A private, self-hosted command center for tasks, recurring work, habits,
projects, personal media, and finance. Luigi Web is a server-rendered FastAPI
application designed to run alongside
the shared LuigiBot repository.

The installable host now composes feature packages behind a shared Jinja/HTMX
shell. All 11 built-ins remain enabled by default; smaller selections can run
without PostgreSQL. The modular refactor preserves existing data ownership,
authentication, and source deployment entry points.

## Highlights

- **Tasks:** Kanban and compact list views, Quick Add, filters, snooze, Undo,
  completion triggers, interval/weekday/monthly-position recurrence, projects,
  archive, dependencies, local reminders, a migration-aware Activity timeline,
  and projected recurring occurrences on Month/Agenda calendar views.
- **Discipline:** daily completion controls, yearly heatmaps, streaks, weekly
  progress, and shared data with LuigiBot.
- **Projects:** named-project timeline with scheduled and unscheduled work.
- **Games and shows:** shared Game'N'Watch Google Sheet, metadata search,
  covers, ratings, statuses, Steam playtime, achievements, and local Insights
  charts/tables with backlog health and health-aware weighted picks.
- **Trading Cards:** local catalogs, decks, collections, and price history in
  one isolated app-owned database. See [docs/trading-cards.md](docs/trading-cards.md).
- **Characters:** private D&D 5e (2014) and Pathfinder 2e sheets with live
  resources, abilities, saves, skills, spells, equipment, notes, and cloned
  level-path states, plus a reusable local rules library and guided class
  progression.
- **Finance:** separately unlocked accounts, transactions, budgets,
  investments, net worth, CSV import, reports, alerts, audit history, and
  exports. Finance data is isolated from LuigiBot and the LLM.
- **Assistant:** optional GitHub Copilot subscription or OpenAI-compatible chat
  with an allow-listed task tool registry. Finance is intentionally excluded.
- **Operations:** guided Daily/Weekly Review, verified preview-first shared-task
  restore, integration health, environment editor, self-update, local Feedback,
  approved-feedback draft PR automation, bulk task actions, isolated branch
  Preview, responsive navigation, and `Ctrl+K` global commands.
- **Modules:** authenticated catalog and restart-time selection, packaged
  templates/assets, and an API for deployment-approved external packages.

## Screenshots

Production screenshot filenames and privacy-safe capture guidance are documented
in [`docs/screenshots/README.md`](docs/screenshots/README.md). The image block
below stays commented until those files are added, so GitHub never displays
broken images.

<!-- Uncomment after adding the PNG files.

| Home dashboard | Tasks board |
|---|---|
| ![Home dashboard](docs/screenshots/home-dashboard.png) | ![Tasks board](docs/screenshots/tasks-board.png) |

| Tasks list | Calendar |
|---|---|
| ![Tasks list](docs/screenshots/tasks-list.png) | ![Calendar](docs/screenshots/calendar.png) |

| Games and ratings | Command palette |
|---|---|
| ![Games](docs/screenshots/games.png) | ![Command palette](docs/screenshots/command-palette.png) |

![Finance dashboard with synthetic data](docs/screenshots/finance.png)

![Mobile Tasks](docs/screenshots/mobile-tasks.png)

-->

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
pip install -e . --no-deps
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
selection, wheel installation, and the independently installable example.

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
remove bundled compatibility code: Tasks and Discipline still share the
LuigiBot adapter when those features are used.

Selection priority is an explicit registry argument, then `LUIGI_WEB_MODULES`,
then the saved module-selection file, then all built-ins. The authenticated
`/modules` page filters the catalog and saves validated selections atomically
for the **next restart**, without hot reload or deployment. When
`LUIGI_WEB_MODULES` is set, selection is deployment-managed and read-only there.

External modules require installed entry points approved through the
deployment-only `LUIGI_WEB_EXTERNAL_MODULES` allow-list, followed by selection.
Installation alone never enables one. The GUI cannot install Git URLs or
change this allow-list. These modules are trusted code with the host process's
privileges, not sandboxed plugins.

See [docs/modules.md](docs/modules.md) for configuration and the versioned API,
and [examples/example-module/README.md](examples/example-module/README.md) for
the real independently installable example. This is a shipped framework and
example package, not a claim that all features have separate published repos.

## Configuration

Copy [`.env.example`](.env.example) and keep the real file outside version
control. Core settings:

| Variable | Purpose |
|---|---|
| `LUIGI_WEB_MODULES` | Comma-separated IDs or `none`; overrides saved selection and makes GUI selection read-only |
| `LUIGI_WEB_MODULES_FILE` | Optional selection-file path; defaults to `DATA_DIR/modules.json` |
| `LUIGI_WEB_EXTERNAL_MODULES` | Deployment-only allow-list of installed `luigi_web.modules` entry-point names |
| `LUIGI_WEB_DATA_DIR` | Optional writable defaults root; source `data/` or platform per-user storage when installed |
| `LUIGI_WEB_PG_*` | Shared LuigiBot PostgreSQL connection |
| `LUIGI_WEB_PG_CONNECT_TIMEOUT` | Connection timeout in seconds, clamped to `1`–`30` (default `5`) |
| `LUIGI_WEB_UI_TOKEN` | Main application login token |
| `LUIGI_WEB_FINANCE_TOKEN` | Separate Finance unlock token |
| `LUIGI_WEB_SECURE_COOKIES` | Set to `1` behind HTTPS |
| `LUIGI_WEB_TIMEZONE` | IANA timezone for user-facing dates (default `America/New_York`) |
| `LUIGI_WEB_DAY_CUTOFF` | Local `HH:MM` cutoff for previous-day completion (default `04:00`) |
| `LUIGI_WEB_FINANCE_DB` | App-owned Finance SQLite path |
| `LUIGI_WEB_FINANCE_BASE_CURRENCY` | ISO currency used for reports |
| `LUIGI_WEB_REVIEW_DB` | App-owned Daily/Weekly Review SQLite path |
| `LUIGI_WEB_OPERATIONS_DB` | App-owned task dependencies and reminders SQLite path |
| `LUIGI_WEB_RPG_DB` | App-owned character sheets and level paths SQLite path |
| `LUIGI_WEB_LLM_*` | Optional GitHub Copilot or OpenAI-compatible assistant |
| `LUIGI_WEB_COPILOT_HOME` | Writable cache for the bundled Copilot runtime |
| `LUIGI_WEB_GNW_*` | Optional Game'N'Watch Google Sheet |
| `LUIGI_WEB_STEAM_*` | Optional Steam progress integration |
| `LUIGI_WEB_YOUTUBE_API_KEY` | Optional playlist search |

Until the shared `task_events` migration is installed, Calendar still displays
currently completed task rows using `completed_time`, converted from legacy UTC
timestamps into `LUIGI_WEB_TIMEZONE`. It labels this as limited history because
older recurring completions cannot be reconstructed after reactivation.

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
coding worker. The optional worker creates draft pull requests, never merges
or deploys them. See
[docs/autonomous-maintainer.md](docs/autonomous-maintainer.md) for the trust
boundary and deployment requirements.

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
python -m unittest discover -s tests -v
python scripts/validate_repo.py
git diff --check
```

Application code lives in [luigi_web](luigi_web), with core resources under
[luigi_web/core](luigi_web/core) and feature routes/templates under
[luigi_web/modules](luigi_web/modules). Root [app.py](app.py) intentionally
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
React/TypeScript build is required. The reasons for retaining the current
stack and the remaining shared-service work are in
[docs/architecture.md](docs/architecture.md).

For a disposable local UI preview, run:

```powershell
python scripts/preview_workspace.py --check
python scripts/preview_workspace.py
```

The helper prints an available loopback URL, uses synthetic records and
temporary databases, and blocks deployment actions and external refreshes.
Task changes are read-only; module selection and Cards/Characters changes stay
in disposable storage. It does not exercise production integrations or apply
saved module selections to a running process. Stop it with Ctrl+C when done.
The latest checked workflows are recorded in
[docs/modular-validation-2026-09-17.md](docs/modular-validation-2026-09-17.md).

Repository guidance:

- [`AGENTS.md`](AGENTS.md) — coding-agent rules, privacy constraints, and
  validation requirements
- [`docs/architecture.md`](docs/architecture.md) — storage boundaries and
  technical design
- [docs/modules.md](docs/modules.md) - selection, packaging, and trusted
  external-module contracts
- [examples/example-module/README.md](examples/example-module/README.md) -
  editable-install demo and independent wheel verification
- [`docs/discipline-v2-plan.md`](docs/discipline-v2-plan.md) — coordinated
  LuigiBot Discipline migration
- [`docs/task-events-plan.md`](docs/task-events-plan.md) — shared completion
  and task activity event contract
- [`docs/preview-deployment.md`](docs/preview-deployment.md) — isolated Git
  branch Preview worktree/service setup
- [`docs/autonomous-maintainer.md`](docs/autonomous-maintainer.md) — reviewed
  Feedback-to-draft-PR worker and daily timer
- [`docs/screenshots/README.md`](docs/screenshots/README.md) — public screenshot
  capture checklist
- [`SECURITY.md`](SECURITY.md) — security and sensitive-data policy

## License

No license has been declared for Luigi Web itself. Runtime-imported SRD 5.1
material retains its CC BY 4.0 attribution described above. Add a project
license before accepting external contributions.
