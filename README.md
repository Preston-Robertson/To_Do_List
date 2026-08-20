# Luigi Web

A private, self-hosted command center for tasks, recurring work, habits,
projects, personal media, and finance. Luigi Web is a server-rendered FastAPI
application designed to run alongside
the shared LuigiBot repository.

## Highlights

- **Tasks:** Kanban and compact list views, Quick Add, filters, snooze, Undo,
  completion triggers, interval/weekday/monthly-position recurrence, projects,
  archive, a migration-aware Activity timeline, and projected recurring
  occurrences on Month/Agenda calendar views.
- **Discipline:** daily completion controls, yearly heatmaps, streaks, weekly
  progress, and shared data with LuigiBot.
- **Projects:** named-project timeline with scheduled and unscheduled work.
- **Games and shows:** shared Game'N'Watch Google Sheet, metadata search,
  covers, ratings, statuses, Steam playtime, achievements, and local Insights
  charts/tables with backlog health and health-aware weighted picks.
- **Finance:** separately unlocked accounts, transactions, budgets,
  investments, net worth, CSV import, reports, alerts, audit history, and
  exports. Finance data is isolated from LuigiBot and the LLM.
- **Assistant:** optional GitHub Copilot subscription or OpenAI-compatible chat
  with an allow-listed task tool registry. Finance is intentionally excluded.
- **Operations:** integration health, environment editor, backup/export,
  self-update, local Feedback, bulk task actions, isolated branch Preview,
  responsive navigation, and `Ctrl+K` global commands.

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

## Quick start

Requirements:

- Python 3.11+
- a LuigiBot PostgreSQL database at schema version 2
- optional Google Sheets credentials for Games/Shows

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Set the placeholder values in `.env`, load them into the process environment,
then run:

```powershell
python -m uvicorn app:app --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080` and sign in with `LUIGI_WEB_UI_TOKEN`. Finance
requires the separate `LUIGI_WEB_FINANCE_TOKEN`.

## Configuration

Copy [`.env.example`](.env.example) and keep the real file outside version
control. Core settings:

| Variable | Purpose |
|---|---|
| `LUIGI_WEB_PG_*` | Shared LuigiBot PostgreSQL connection |
| `LUIGI_WEB_UI_TOKEN` | Main application login token |
| `LUIGI_WEB_FINANCE_TOKEN` | Separate Finance unlock token |
| `LUIGI_WEB_SECURE_COOKIES` | Set to `1` behind HTTPS |
| `LUIGI_WEB_TIMEZONE` | IANA timezone for user-facing dates (default `America/New_York`) |
| `LUIGI_WEB_DAY_CUTOFF` | Local `HH:MM` cutoff for previous-day completion (default `04:00`) |
| `LUIGI_WEB_FINANCE_DB` | App-owned Finance SQLite path |
| `LUIGI_WEB_FINANCE_BASE_CURRENCY` | ISO currency used for reports |
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

## Future features

- **Feedback:** an opt-in in-app feedback workflow with a review step before
  anything leaves the server and automatic removal of sensitive fields.
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

Run the offline regression suite:

```powershell
python -m unittest discover -s tests -v
```

Application code lives in the `luigi_web/` package. Root `app.py` intentionally
remains as the stable `uvicorn app:app` compatibility entry point used by local
development and the supplied systemd service.

The tests use synthetic data only and do not require live PostgreSQL, Google,
Steam, bank, or LLM credentials.

Repository guidance:

- [`AGENTS.md`](AGENTS.md) — coding-agent rules, privacy constraints, and
  validation requirements
- [`docs/architecture.md`](docs/architecture.md) — storage boundaries and
  technical design
- [`docs/discipline-v2-plan.md`](docs/discipline-v2-plan.md) — coordinated
  LuigiBot Discipline migration
- [`docs/task-events-plan.md`](docs/task-events-plan.md) — shared completion
  and task activity event contract
- [`docs/preview-deployment.md`](docs/preview-deployment.md) — isolated Git
  branch Preview worktree/service setup
- [`docs/screenshots/README.md`](docs/screenshots/README.md) — public screenshot
  capture checklist
- [`SECURITY.md`](SECURITY.md) — security and sensitive-data policy

## License

No license has been declared. Add one before accepting external contributions.
