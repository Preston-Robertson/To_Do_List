# LuigiBot Web GUI (`luigi-web`)

Server-rendered FastAPI + Jinja2 web GUI for the
[LuigiBot](https://github.com/Preston-Robertson/LuigiBot) to-do system. It is
the **second read-write client** of the shared Postgres database `luigi_todo`;
LuigiBot is the schema-owning first client. Both write concurrently —
Postgres/MVCC keeps things safe.

> **Prerequisite:** The `luigi_todo` DB must be at `schema_version = 2` (the four
> list tables must have a `uuid` column). The app refuses to serve traffic if
> `schema_version < 2`.

---

## Screenshots

Production screenshot filenames, viewport sizes, privacy checks, and capture
states are defined in [`docs/screenshots/README.md`](docs/screenshots/README.md).
The image markup below is intentionally commented out so the GitHub README
never shows broken images before the deployed UI is captured.

Recommended hero captures are **Home**, **Tasks Board**, **Tasks List**,
**Calendar**, **Games with rating**, and the **Ctrl+K command palette**, plus
one mobile Tasks image. Keeping those filenames stable means adding the images
later requires only uncommenting the prepared Markdown block.

<!-- Uncomment after the PNG files are added under docs/screenshots/.

| Home dashboard | Tasks · Board |
|---|---|
| ![LuigiBot Home dashboard](docs/screenshots/home-dashboard.png) | ![Tasks Kanban board](docs/screenshots/tasks-board.png) |

| Tasks · List | Calendar |
|---|---|
| ![Compact Tasks list](docs/screenshots/tasks-list.png) | ![Task calendar](docs/screenshots/calendar.png) |

| Games and ratings | Global command palette |
|---|---|
| ![Games board with rating control](docs/screenshots/games.png) | ![Global search and command palette](docs/screenshots/command-palette.png) |

![Mobile Tasks view](docs/screenshots/mobile-tasks.png)

-->

---

## UI approach

**Application shell.** Authenticated pages use a responsive, grouped left
sidebar rather than a crowded top-tab row. Desktop navigation is organized as
**Focus** (Home, Tasks, Calendar), **Planning** (Projects, Discipline, Archive),
**Media** (Games, Shows), and **System** (Admin). The sidebar collapse choice
persists in `localStorage.luigi.sidebar.collapsed`; below 900 px it becomes an
accessible temporary drawer with a backdrop and Escape-to-close behavior.
Page headers, buttons, form controls, cards, metadata chips, and focus states
share one restrained visual-token system in `static/css/app.css`.

**Home dashboard** at `/home` — customizable widget grid summarizing what needs
attention right now. Each widget has its own accent stripe and scrolls
internally, so an item with hundreds of rows never pushes the page taller.

| Widget | Query | Accent |
|---|---|---|
| **Overdue** | `tasks + recurring_tasks` with `due_date < today`, `completed = 0`, `status != 'Completed'` (both flags checked — some rows can drift out of sync when a status change goes through a path that skips the completed flag) | red |
| **Upcoming · 7 days** | open items with `due_date` in `[today, today+7]`, same completion filter as Overdue | blue |
| **Open Tasks** | `Not Started` or `In Progress`, priority DESC / due ASC | primary |
| **Discipline · Today** | active disciplines with no completion for today; inline Done button POSTs to `/discipline/toggle` | amber |
| **Discipline · Streaks** | active disciplines sorted by `current_streak` DESC | orange |
| **Follow-ups** | highest-priority `follow_up_tasks` with their trigger shown inline | teal |
| **Recently completed** | last 8 completed items across `tasks` + `recurring_tasks` | green |
| **Discipline · This week** | Mon–Sun bar chart of `discipline_completions` | green |
| **Tasks completed · This week** | Mon–Sun bar chart of items whose `completed_time` falls in the current ISO week | violet |
| **Recent activity** | last N events across `tasks` + `recurring_tasks` + `discipline_completions` + `follow_up_tasks`, derived from existing timestamp columns (no audit table) | slate |
| **Weekly review** | last 7 days ending yesterday: completions total, discipline days N/7, carried-over overdue, next-week upcoming, plus a top-categories bar list | sky |
| **Currently Playing** | up to 8 Game'N'Watch games whose status is `playing`; shown when Google Sheets is configured | cyan |
| **Currently Watching** | up to 8 Game'N'Watch shows whose status is `watching`; shown when Google Sheets is configured | purple |

Above the widget grid there's an ambient **"Streaks at risk today" banner**
(amber) that surfaces active disciplines whose `current_streak > 0` and whose
last completion is at least `ceil(7 / frequency_per_week)` days ago — i.e.
the streak breaks at midnight if today's hit doesn't happen. Each row has an
inline **Done** button (POSTs to `/discipline/toggle`) and the whole banner
has a dismiss-for-today control keyed on a `YYYY-MM-DD:<count>` signature
(`localStorage.luigi.atRiskDismiss`) so a new day or a changed count re-shows
it. The banner never renders when the list is empty.

Each widget can be shown/hidden via the "Customize widgets" dropdown; state
is persisted in `localStorage` per browser (key `luigi.home.hiddenWidgets`).
Widget queries are read-only and bounded by row limits and/or date windows;
they live in `db.py`
(`list_open_tasks`, `list_overdue_tasks`, `list_upcoming_tasks`,
`list_recent_completions`, `list_discipline_streaks`,
`list_disciplines_pending_today`, `list_disciplines_at_risk`,
`list_follow_ups_preview`, `weekly_discipline_counts`,
`weekly_task_completion_counts`, `list_recent_activity`, `weekly_review`).

**Kanban board by status.** One-off and recurring items share the Tasks 3×2
Kanban board. Recurring cards are marked with a `↻ Recurring` chip and retain
their recurrence schedule; the **+ Task** and **+ Recurring** buttons create
the appropriate row type. The legacy `/recurring` page redirects to `/tasks`.
The column layout is:

    Row 1 :  Not Started  |  In Progress  |  Completed
    Row 2 :  Blocked      |  Hiatus       |  Pending

The board is height-capped to the viewport — each column is an independent
scroll container, so a Completed column with hundreds of cards never blows up
the page. Drag a card between columns to change status. Use **Edit** to change
all fields in a modal. HTMX handles inline updates; SortableJS handles
drag-and-drop.

Each card has a **Snooze ▾** menu (+1d / +3d / +1w / +2w) that POSTs to
`/tasks/{uuid}/snooze` (or `/recurring/{uuid}/snooze`) and swaps the card in
place. Snooze math uses `max(today, current_due) + days`, so overdue items
always defer from today rather than from a stale due date.

**Undo toast.** Every complete / delete / snooze (on both `tasks` and
`recurring_tasks`) plus discipline delete snapshots the pre-mutation row into
a server-side in-memory queue (`_UNDO_QUEUE` in `app.py`, capped at 64
entries with a 12 s TTL, guarded by a `threading.Lock`) and emits an
`HX-Trigger: showUndo` event carrying `{op_id, label, ttl_ms}`. The client
writes the pending entry to `localStorage.luigi.pendingUndo` **synchronously
in the same tick as the `reloadBoard` trigger** so the toast survives the
ensuing full-page reload; `restoreUndoToast()` re-renders it on
`DOMContentLoaded`. Clicking **Undo** fires `POST /undo/{op_id}`, which pops
the entry and dispatches on `table`: task-like snapshots go through
`db.restore_task_row(table, snapshot)` (one idempotent UPDATE-or-INSERT that
reverses complete, delete, and snooze uniformly), and discipline snapshots go
through `db.restore_discipline_row(snapshot)` (puts back the
`discipline_list` row **and** re-inserts every `discipline_completions` row
through an existence-guarded insert, so the streak history returns intact). In
every case the row's original `uuid` is reused, so any references remain
valid. Expired ops return `410 Gone`.

**New-task auto-refresh.** After the New Task modal POSTs successfully, the
response carries an `HX-Trigger: reloadBoard` so the newly-created card shows
up in its column immediately without the user having to reload manually.

**Quick Add.** The Tasks header has a compact form for a one-off task name,
due date, priority, and optional project. It posts to `POST /tasks/quick` and
refreshes the board after a confirmed insert; the full modal remains available
for categories, links, and recurrence details.

**Native date picker with presets.** The `Due date` field in the task modal
uses `<input type="date">` (so you get the OS-native calendar GUI — no
typing) plus quick chips: **Today · Tomorrow · +1w · +2w · Clear**. The
active preset stays highlighted. Wired in `initDatePickers()` in
`static/js/app.js`; re-initialized after every HTMX swap so modals opened
later still get the chips.

Above each board is a **filter bar** with free-text search, a status /
priority / category dropdown, and a **smart-list** picker with built-ins:
*Overdue*, *Due this week*, *No due date*, *High priority (≥ 5)*, and
*Completed this week*. Text search covers title, project, category, group, and
sub-group. Filtering is 100% client-side — the templates emit
`data-*` attributes on each card and `static/js/app.js` toggles visibility
in the DOM. Named filters can be saved ("☆ Save current") and reapplied per
endpoint; state lives in `localStorage` under
`luigi.tasks.savedFilters` and `luigi.tasks.activeFilter.<endpoint>`.

The DB-level status enum stays in its canonical order
(`db.STATUS_VALUES`); the display order is a separate constant
(`db.STATUS_DISPLAY_ORDER`) so reordering the board never changes what the
backend accepts.

**Board/List views.** Tasks can switch between the visual Kanban and a compact
table-style List without another server request. The selection persists under
`localStorage.luigi.tasks.view`; both views share the same text, smart-list,
priority, and category filters. List rows provide inline status changes plus
the same complete, edit, snooze, archive, and delete operations as cards.

**Drawers and overflow actions.** HTMX still targets the existing `#modal-body`
contract, but edit/create/search content is presented as a responsive right-side
drawer so the underlying board or list remains visible. Task and media cards
keep their primary operation visible while secondary and destructive actions
live under a consistent `•••` menu; outside clicks and completed HTMX swaps
close open menus automatically.

**Command palette and feedback.** `Ctrl+K` opens a keyboard-navigable global
palette for page navigation, creation actions, and grouped search across tasks,
disciplines, games, and shows. HTMX requests drive a thin global progress bar;
drawers display skeleton content while loading; successful writes emit a
consistent toast that survives an immediate refresh; error and Undo toasts
remain independent. Motion is subtle and disabled automatically when the
browser requests reduced motion.

Other views:

* **Projects** — named-project Gantt chart at `/projects`. Tasks have an
  optional web-owned `project` field (for example, `Renovate Kitchen`), and
  project chips narrow the timeline. If the DB role cannot add that optional
  column, project assignments are retained in the app-managed, gitignored
  `task-web-metadata.json` fallback. The page
  renders a two-pane view: a fixed names column on the left and a scrolling
  SVG timeline on the right. Bars derive their span from `start_time` (or
  `task_creation` as a fallback) through `due_date`; items without a
  `due_date` drop into an "Unscheduled" section below the chart. The
  header draws month gridlines/labels and a dashed "today" marker; bars are
  colored by status. Clicking a task name opens the same edit modal used
  on the Kanban.
* **Calendar** — month grid at `/calendar`, showing one-off and recurring tasks
  by due date. Status-colored task pills open the normal edit modal; previous,
  next, and Today controls make it useful as a schedule view.
* **Archive** — completed one-off and recurring cards can be moved out of
  active boards and restored from `/archive`. It uses an optional web-owned
  `archived` column and never deletes task history; the same app-managed
  metadata fallback is used when
  the database role cannot add the optional column. Archived rows are omitted
  from Tasks, dashboard task lists, Calendar, Projects, chat task search, and
  recurring reactivation until restored.
* **Discipline** — each active item has an explicit UUID-backed **Done today** /
  **Undo today** action, plus a GitHub-style yearly heatmap for marking or
  clearing any past date. The server resolves the canonical task/category by
  UUID and verifies the legacy completion write before refreshing the page.
* **Follow-ups** — rule table at `/follow-ups`, also loaded into the Tasks
  modal through the **Follow-up rules** button.
* **Games** / **Shows** — Kanban-by-status boards backed by the **Game'N'Watch**
  Google Sheet (see *Game'N'Watch integration* below). Profile selector, per-card
  status change + edit modal, and a weighted “Surprise me” random picker.
* **Admin** — runtime info + self-update / restart controls + JSON backup
  export + a paste-in Game'N'Watch credentials panel (see below). Its
  read-only **Integration health** cards query PostgreSQL and Google Sheets,
  run a fully rolled-back Discipline schema/permission write check, probe the
  Steam Store, TVMaze, and AniList APIs, report Steam-progress / YouTube / LLM
  configuration, and verify Git plus env-file access. Results include response
  times and actionable errors—no terminal diagnostics needed.

### Future UI directions (noted for later)

* **Future Projects option — task-flow web.** Potentially rework the
  `/projects` tab into a free-flowing dependency graph, inspired by the
  Azure Machine Learning Designer canvas but time-aware:
  * **X-axis = timeline (dates)**, not "depth". Nodes snap to their
    `due_date` (or `start_time`..`due_date` span), so the whole graph
    reads left → right in chronological order.
  * **Nodes = task cards** with title, status chip, priority, and duration
    bar. Drag a node to reschedule (updates `start_time` / `due_date` via
    the existing task-update route).
  * **Edges = prerequisites.** Click-drag from one node's right-hand port
    to another's left-hand port to declare *"A must finish before B"*.
  * **Blocked visualisation.** Any node with at least one incomplete
    upstream prerequisite is auto-rendered as **Blocked** — greyed bar,
    lock icon, edge tinted red. The task's canonical `status` is not
    silently rewritten; the block state is derived on render, so
    completing the upstream task instantly un-blocks the downstream one
    on the next refresh.
  * **Zoom + pan** on the timeline; sidebar lists the currently-selected
    category set (same picker as today's Gantt).
  * **Schema impact.** Needs one new table on LuigiBot's side:
    `task_dependencies(uuid PRIMARY KEY, task_uuid, blocks_uuid,
    created_at)` with `UNIQUE(task_uuid, blocks_uuid)` and both FKs
    pointing at `tasks.uuid` / `recurring_tasks.uuid`. GUI stays
    DDL-free — LuigiBot ships the migration; this app just reads/writes
    the table.
* **In-app feedback:** a persistent, unobtrusive **Feedback** action for Bug,
  Idea, and General feedback. A future implementation should capture the
  current page, app version/commit, optional screenshot, and user message;
  preview exactly what will be sent; redact secrets; and route submissions to
  a configurable GitHub Issues repository or an app-owned feedback inbox.
* **Discipline v2:** replace task-text-linked completion history with shared,
  UUID-keyed Postgres definitions/events used by both LuigiBot and this GUI.
  The coordinated schema, migration, validation, and cutover plan is documented
  in [`docs/discipline-v2-plan.md`](docs/discipline-v2-plan.md).

These are additive — the DB layer and route shape don't need to change to add
them; only new templates + route variants (and, for Option C, one new table).

---

## Data contract (short version)

The web SQL adapter is centralized in `db.py`; authoritative schema creation
and versioned migrations live in LuigiBot's `bot_modules/db.py`. Hard rules the
GUI must obey:

* Never insert an explicit `id` — PKs are `GENERATED ALWAYS AS IDENTITY`.
* `uuid` is the durable row handle. All `UPDATE`/`DELETE` are scoped `WHERE uuid = :uuid`.
* Booleans are `INTEGER 0/1` (`completed`, `recurring`, `active`).
* Dates and datetimes are ISO-8601 `TEXT` (`YYYY-MM-DD` or full ISO).
* Intentional SQL spellings: `catagory` (sic), `sub_group` in tasks, `subgroup`
  in follow-ups. The GUI talks SQL directly so it uses these names as-is.
* `discipline_completions` has `UNIQUE(task, completed_date)`. Mark uses an
  existence-guarded `INSERT ... SELECT ... WHERE NOT EXISTS`; unmark uses
  `DELETE ... WHERE task AND completed_date`. Both writes verify their final
  state before reporting success.
* No whole-table rewrites. Ever.
* LuigiBot owns its schema and schema version. At startup the GUI only attempts
  idempotent, nullable web-owned columns (`recurring_days`, `project`,
  `archived`) and never bumps `schema_version`. Missing ALTER privileges are
  non-fatal: weekday scheduling hides itself, while project/archive state uses
  the app-managed `task-web-metadata.json` fallback.

---

## Local dev

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Runtime configuration comes from process environment variables. Set the
# required database values and login token in this shell before starting.
$env:LUIGI_WEB_PG_HOST="10.0.0.202"
$env:LUIGI_WEB_PG_PORT="5432"
$env:LUIGI_WEB_PG_DB="luigi_todo"
$env:LUIGI_WEB_PG_USER="luigi_web"
$env:LUIGI_WEB_PG_PASSWORD="<database password>"
$env:LUIGI_WEB_UI_TOKEN="<login token>"

# smoke-test the DB connection (read-only)
python scripts\smoke_test.py

# run
uvicorn app:app --reload --host 0.0.0.0 --port 8080
```

Open `http://localhost:8080`. You'll be redirected to `/login`; enter the value
of `LUIGI_WEB_UI_TOKEN` to get a session cookie.

### Validation

The offline regression suite does not require PostgreSQL, Google credentials,
a Steam key, or an LLM endpoint:

```powershell
python -m unittest discover -s tests -v
```

It covers authentication, database URL construction, recurring schedule math,
discipline failure isolation, combined task rendering, project/calendar
shaping, Game'N'Watch URL/header handling, catalog metadata,
Sheet insertion, and LLM history/tool limits. Admin's **Integration health**
panel provides the corresponding read-only checks against configured live
services.

---

## Environment variables

| Var | Purpose |
|---|---|
| `LUIGI_WEB_PG_HOST` | Postgres host (LXC 104 → `10.0.0.202`) |
| `LUIGI_WEB_PG_PORT` | Postgres port (`5432`) |
| `LUIGI_WEB_PG_DB`   | Database name (`luigi_todo`) |
| `LUIGI_WEB_PG_USER` | DB role (`luigi_web`) |
| `LUIGI_WEB_PG_PASSWORD` | DB password — **env-only** |
| `LUIGI_WEB_UI_TOKEN` | Shared login token — **env-only** |
| `LUIGI_WEB_BIND` | Uvicorn bind address (default `0.0.0.0`) |
| `LUIGI_WEB_PORT` | Uvicorn port (default `8080`) |
| `LUIGI_WEB_ENV_FILE` | Path the Admin env editor writes. Defaults to `<repo>/.env`; recommended LXC value is `/opt/luigi-web/luigi.env` |
| `LUIGI_WEB_LLM_PROVIDER` | `openai` (default) or `disabled` |
| `LUIGI_WEB_LLM_BASE_URL` | OpenAI-compatible endpoint. Default `https://models.github.ai/inference` (GitHub Models) |
| `LUIGI_WEB_LLM_API_KEY` | Chat panel is disabled when blank. GitHub PAT with `models:read` for GitHub Models |
| `LUIGI_WEB_LLM_MODEL` | Default `openai/gpt-4o-mini` |
| `LUIGI_WEB_LLM_TIMEOUT` | HTTP timeout in seconds (default `60`) |
| `LUIGI_WEB_LLM_MAX_TOOL_ITERATIONS` | Cap on tool round-trips per message (default `5`) |
| `LUIGI_WEB_GNW_SHEET_ID` | Game'N'Watch Google Sheet ID or full sheet URL. Blank hides the Games/Shows tabs |
| `LUIGI_WEB_GNW_CREDS_FILE` | Path to the service-account `credentials.json`. Leave **blank** to use the app-managed path (`<repo>/gnw-credentials.json`) that the Admin page writes to |
| `LUIGI_WEB_STEAM_API_KEY` | Optional Steam Web API key for personal playtime and achievements. Steam store search/metadata works without it |
| `LUIGI_WEB_STEAM_ID` | Steam ID64 used with the Steam API key for owned-game playtime and achievement progress |
| `LUIGI_WEB_YOUTUBE_API_KEY` | Optional YouTube Data API key that adds playlist results to Add Show |

Secrets **must** stay uncommitted — in `/opt/luigi-web/luigi.env` on the LXC
(recommended mode `600`, owner `luigi-web`), in the process environment for
local development, or in the legacy `/etc/luigi-web.env`. `.gitignore` blocks
`.env`, service-account credentials, and app-managed task metadata.

---

## Routes

Unauthenticated:
* `GET /healthz` → `{"status":"ok","schema_version":N}`
* `GET /login`, `POST /login`, `POST /logout`

Authenticated (session cookie, or `?token=` / `Authorization: Bearer`):
* `GET  /`                → redirects to `/home`
* `GET  /command-palette?q=TEXT` → quick actions/navigation when blank, or
  grouped task, discipline, game, and show search results for `Ctrl+K`
* `GET  /home`            → widget dashboard (overdue, upcoming, open tasks,
  discipline today, discipline streaks, follow-ups, recent completions,
  weekly discipline chart, weekly tasks-completed chart, recent activity,
  weekly review, and optional currently-playing/watching widgets)
* `GET  /tasks`           → combined one-off + recurring Kanban board
* `POST /tasks`           → create
* `POST /tasks/quick`     → Quick Add a one-off task
* `GET  /tasks/new`       → task creation modal partial
* `GET  /tasks/{uuid}/edit`   → modal edit form (HTMX partial)
* `POST /tasks/{uuid}`    → update
* `POST /tasks/{uuid}/status` → drag-drop status change
* `POST /tasks/{uuid}/complete` → toggle `completed` + `completed_time`
* `POST /tasks/{uuid}/snooze` → defer `due_date` by `days` (form field);
  returns the re-rendered card partial for an HTMX swap
* `POST /tasks/{uuid}/archive`, `POST /tasks/{uuid}/restore` → move a
  completed task into/out of Archive
* `POST /tasks/{uuid}/delete` → delete
* `GET  /recurring` → compatibility redirect to `/tasks`; recurring create,
  edit, status, complete, snooze, archive, restore, and delete routes remain
  under `/recurring/*` for the consolidated board's recurring cards
* `POST /recurring`, `GET /recurring/new`,
  `GET /recurring/{uuid}/edit`, `POST /recurring/{uuid}` → recurring create
  and edit flows
* `POST /recurring/{uuid}/status`, `POST /recurring/{uuid}/complete`,
  `POST /recurring/{uuid}/snooze`, `POST /recurring/{uuid}/archive`,
  `POST /recurring/{uuid}/restore`, `POST /recurring/{uuid}/delete` →
  recurring card mutations
* `GET  /projects?project=X&project=Y&include_recurring=1` → named-project
  Gantt chart; legacy `?catagory=` bookmarks remain accepted
* `GET  /calendar?month=YYYY-MM` → month calendar of task due dates
* `GET  /archive` → archived one-off and recurring task history with restore
* `GET  /discipline?year=YYYY` → yearly heatmaps
* `POST /discipline`      → create
* `GET  /discipline/new`  → creation modal partial
* `GET  /discipline/{uuid}/edit`, `POST /discipline/{uuid}` → update
* `POST /discipline/{uuid}/deactivate` → set `active=0`
* `POST /discipline/{uuid}/delete` → hard-delete the discipline **and** all
  its `discipline_completions` rows, then emit `HX-Trigger: showUndo` so
  the client toast can restore both within the 12 s window
* `POST /discipline/toggle` → mark/unmark a day (also used by the Home
  discipline widget's "Done" button)
* `GET  /follow-ups`      → standalone rules table
* `GET  /follow-ups/panel` → rules-manager partial loaded from Tasks
* `GET  /follow-ups/new`, `POST /follow-ups`,
  `GET /follow-ups/{uuid}/edit`, `POST /follow-ups/{uuid}`,
  `POST /follow-ups/{uuid}/delete` → follow-up rule CRUD
* `GET  /games`, `GET /shows` → Game'N'Watch Kanban board (optional
  `?profile=NAME`). Renders a “not configured” notice when the sheet id /
  credentials are missing.
* `GET  /gnw/{section}/new` → Add Game/Show modal
* `POST /gnw/{section}/search` → Steam catalog search for games; resilient
  TVMaze + AniList + optional YouTube playlist search for shows
* `POST /gnw/{section}/add` → write a catalog result or manual item into the
  shared Google Sheet
* `POST /gnw/{section}/status` → change an item's status (form: `profile`,
  `title`, `status`); writes the sheet, returns `204 + HX-Refresh`
* `GET  /gnw/{section}/edit?profile=&title=` → modal edit form
* `POST /gnw/{section}/update` → write editable fields back to the sheet
* `POST /gnw/{section}/pick`   → weighted random pick partial
* `GET  /gnw/games/steam-stats?profile=&title=&app_id=` → Steam-owned
  playtime, achievement progress, and locked-achievement preview; refreshes
  the sheet's `Hours Played` field
* `POST /admin/gnw-credentials` → validate + save a pasted service-account
  `credentials.json` to disk (mode `600`) and hot-reload the Sheets client
* `GET  /admin`           → runtime info + update / restart controls +
  backup export
* `GET  /admin/integrations` → timed, read-only checks for PostgreSQL and
  Google Sheets; a rolled-back Discipline schema/INSERT/DELETE/UPDATE
  capability test; network probes for Steam Store, TVMaze, and AniList;
  configuration status for Steam progress, optional YouTube, and LLM; plus Git
  and managed environment-file checks
* `GET  /admin/backup`    → read-only JSON dump of `tasks`,
  `recurring_tasks`, `follow_up_tasks`, `discipline_list`, and
  `discipline_completions`, plus app-managed project/archive fallback
  metadata. Served with
  `Content-Disposition: attachment; filename="luigi-backup-{stamp}.json"`
  — the Admin page exposes it as a plain download link.
* `POST /admin/update`    → `git fetch` + `git pull --ff-only` +
  `pip install -r requirements.txt` in the repo directory. Returns per-step
  stdout/stderr and exit codes. Does **not** restart the process.
* `POST /admin/restart`   → exits the process; systemd relaunches it (see
  *Self-update* below).
* `POST /admin/env`       → write managed `LUIGI_WEB_*` keys back to the env
  file (path from `LUIGI_WEB_ENV_FILE`). Only keys in
  `env_file.KNOWN_KEYS` are accepted; comments and any other lines in the
  file are preserved untouched. Prefers an atomic replace via a sibling
  tempfile; falls back to an in-place rewrite when only the file (not its
  parent) is writable (e.g. `/etc/luigi-web.env`). LLM keys and
  `LUIGI_WEB_UI_TOKEN` are hot-reloaded into `os.environ` and the running
  provider is rebuilt — no restart needed. DB / bind / port changes still
  require `systemctl restart luigi-web`; the result banner flags which is
  which. Steam and YouTube settings are also hot-reloaded.
* `POST /chat`            → send one user message to the assistant; returns
  an HTML partial containing the user bubble, assistant reply, and any
  tool-call audit entries. Requires `LUIGI_WEB_LLM_API_KEY`.
* `POST /chat/reset`      → clear the in-memory chat history for the caller's
  session.
* `POST /undo/{op_id}`    → pop the queued snapshot for `op_id` and restore
  the row via `db.restore_task_row` (task-like) or
  `db.restore_discipline_row` (discipline_list, including its completions).
  Returns `200` on success (with `HX-Trigger: {reloadBoard, undoCleared}`)
  or `410 Gone` if the op has expired or already been consumed. Covers
  complete / delete / snooze on `tasks` and `recurring_tasks`, plus delete
  on `discipline_list`.

---

## Deployment (CT 105 @ 10.0.0.203)

See `luigi-web.service`. Summary:

1. Unprivileged Debian 12 LXC, `onboot=1`, static IP `10.0.0.203/24`.
2. `apt install python3-venv git` · create user `luigi-web` · clone repo into
   `/opt/luigi-web` · build venv · `pip install -r requirements.txt`.
3. Env file. Recommended: `/opt/luigi-web/luigi.env` owned by `luigi-web`
   (mode `600`) with `EnvironmentFile=/opt/luigi-web/luigi.env` in the unit
   AND `LUIGI_WEB_ENV_FILE=/opt/luigi-web/luigi.env` inside the file itself
   (so the Admin editor targets the same path). This lets the atomic-replace
   save path work without opening `/etc` for group-write. Legacy layout of
   `/etc/luigi-web.env` (mode `640 root:luigi-web`) also works — the editor
   detects the read-only parent and falls back to in-place rewrite.
4. `cp luigi-web.service /etc/systemd/system/` · `systemctl daemon-reload` ·
   `systemctl enable --now luigi-web`.
5. UFW: `ufw allow from 10.0.0.0/24 to any port 8080 proto tcp`; default deny.
6. Confirm: `psql -h 10.0.0.202 -U luigi_web -d luigi_todo -c "SELECT 1;"`.

No NFS. This app runs standalone (its own LXC/systemd), independent of the Bot
Manager.

The systemd unit runs an `ExecStartPre` that reinstalls dependencies on every
start (see next section) — no separate deploy pipeline needed.
It intentionally runs one Uvicorn worker because Undo snapshots, chat history,
live provider configuration, and Google Sheets client state are process-local.

---

## Self-update

The `/admin` page exposes two buttons backed by the routes above:

* **Update** — runs `git fetch`, `git pull --ff-only`, then
  `pip install --no-cache-dir -r requirements.txt` in `/opt/luigi-web`. Renders
  each step's captured stdout/stderr and exit code in the page so a failed pull
  (non-fast-forward, dirty tree, network error) is immediately visible.
* **Restart** — after a short response delay, a worker signals the Uvicorn
  parent and exits. This stops the complete process tree so `systemd` relaunches
  every worker on the new code; single-process development falls back to a
  direct process exit.

Two properties of the unit make this safe and self-healing:

* `Restart=always` — any exit brings the process back.
* `ExecStartPre=-…/pip install --no-cache-dir -r requirements.txt` — every
  start also refreshes Python dependencies. The leading `-` makes the step
  non-fatal, so an offline / PyPI-down box still boots the last known good
  code instead of leaving the service dead. This means a restart alone is
  enough to pick up new packages listed in `requirements.txt`.

Constraints:

* Fast-forward-only pulls. If the working tree has local commits or dirty
  files the update fails loudly — resolve on the LXC with `git status`.
* `pip install` runs as the `luigi-web` user against the in-repo `.venv`.
  Because `ProtectHome=true` hides `~/.cache/pip`, the unit sets
  `PIP_NO_CACHE_DIR=1` and passes `HOME` explicitly.
* No sudo required — the app never asks systemd for anything; it just exits.
* Saving a new shared token through Admin applies it live. Changes made outside
  the application require a service restart.

---

## Non-goals

* No schema-version ownership or destructive DDL. The only DDL is idempotent
  `ADD COLUMN IF NOT EXISTS` for nullable web-owned task metadata, with safe
  degradation when the web role lacks ALTER privileges.
* No optimistic concurrency — last-write-wins scoped by `uuid` (future work).
* No user accounts — single shared token. Rotation = change env + restart.
* No full analytics parity with Game'N'Watch; the GUI focuses on operational
  boards, calendar/project scheduling, Steam progress, and dashboard summaries.
* No changes to LuigiBot.

---

## Assistant (LLM chat panel)

A collapsible chat panel at the top of `/home` lets you drive the app in
natural language: *"add task fix printer priority 3 due tomorrow"*, *"mark
read discipline done"*, *"what's overdue?"*. It's disabled by default. Saving
`LUIGI_WEB_LLM_API_KEY` through Admin rebuilds the provider live; values changed
outside the app take effect after restart.

**Security contract** (see `chat_tools.py` for the exact list):

* The LLM can only invoke a fixed allow-list of Python functions that wrap
  `db.py` helpers. Tool names not in the registry are rejected before any
  Python code runs.
* No shell, no `eval`/`exec`, no filesystem writes, no dynamic imports, no
  arbitrary SQL. The agent cannot modify the app's own code or config.
* Every tool call returns JSON. Every mutating call also shows up in the
  chat as an audit row (`<details>` under the assistant bubble).
* Chat history is in-memory keyed by the session cookie; a restart clears it.
  Nothing is persisted — the DB writes performed by the tools are the audit
  trail.

**Provider abstraction** — `llm.py` speaks the OpenAI `/chat/completions`
format, which works out of the box with:

| Endpoint | `LUIGI_WEB_LLM_BASE_URL` | Key type |
|---|---|---|
| GitHub Models (default) | `https://models.github.ai/inference` | GitHub PAT (`models:read`) |
| OpenAI | `https://api.openai.com/v1` | OpenAI API key |
| Ollama (local) | `http://<host>:11434/v1` | any string (`ollama`) |
| LM Studio (local) | `http://<host>:1234/v1` | any string |
| xAI / DeepSeek / others | their documented base URLs | provider key |

Swap providers by changing `LUIGI_WEB_LLM_BASE_URL` + `LUIGI_WEB_LLM_MODEL`
(and `LUIGI_WEB_LLM_API_KEY`) and restarting. No code change needed.

**Voice input** — the mic button uses the browser's Web Speech API. It stays
disabled unless the browser exposes `SpeechRecognition` *and* the chat panel
is enabled. Chrome/Edge on desktop work; Firefox does not (yet). Dictation
is auto-submitted when it ends.

**Voice output (TTS confirmations).** A 🔊 dropdown in the chat header exposes
three controls, all client-only:

* **Read replies aloud** — toggle (`localStorage.luigi.tts.enabled`).
* **Voice** — dropdown populated from `window.speechSynthesis.getVoices()`,
  re-populated on the `voiceschanged` event because Chrome returns an empty
  list on first call. Selection persists as `voiceURI` under
  `localStorage.luigi.tts.voice`.
* **Test voice** — force-speaks "This is Luigi speaking." with the current
  voice even if the enabled toggle is off, so you can preview before
  committing.

On every HTMX swap into `#chat-log`, the newest `.chat-msg-assistant .chat-bubble`
is cloned, the tool-call `<details>` block is stripped, and the remaining text
is passed to `speechSynthesis.speak(...)`. `speechSynthesis.cancel()` runs
first so rapid replies don't queue up. TTS is disabled by default and the
menu greys itself out when the browser doesn't support `speechSynthesis`.
The system prompt asks the model to keep confirmations to a single short
sentence (TTS-friendly) with any extra detail on a second line.

**Available tools (v2):** `list_open_tasks`, `list_overdue_tasks`,
`list_upcoming_tasks`, `search_tasks`, `suggest_task_fields`, `create_task`,
`complete_task`, `update_task_status`, `delete_task`,
`list_disciplines_pending`, `plan_my_day`, `mark_discipline_done`,
`add_discipline`, `create_follow_up`. Adding a new tool = one Python
function + one `Tool(...)` entry in `chat_tools.build_registry()`.

**`plan_my_day`.** One-shot "what should I do today?" call. Merges four
queries into a single ranked focus list: at-risk streaks first (streaks are
perishable), then overdue tasks ordered by `priority DESC, due_date ASC`,
then tasks due today ordered by `priority DESC`, then remaining pending
disciplines. Each entry carries a `type` (`discipline_at_risk` / `overdue` /
`due_today` / `discipline_pending`) and a human-readable `reason`. The
system prompt routes phrases like *"plan my day"*, *"what should I do
today"*, *"what's on today"* to this tool and forbids re-querying the four
sources separately.

**Auto-fill on create.** The system prompt requires the agent to call
`suggest_task_fields` before `create_task`. That tool runs a case-insensitive
substring search over past tasks (open + completed, both `tasks` and
`recurring_tasks`), **ordered by `task_creation DESC`**, and returns the
most-frequent value in a **recency-weighted window per field** (category ← top
10, priority ← top 5, estimated_time ← top 8, group / sub_group /
relevant_link ← top 10). Recent picks therefore win over stale historical
noise. The agent merges those as defaults, then overrides with anything the
user explicitly said. `due_date` is **never** auto-filled from history — the
user must supply it. Say *"make a Do Laundry task"* and it will pre-fill
category / group / hours from your last few laundry tasks.

**Book-title casing.** Task, category, group, and sub-group names are stored
in book-title capitalization (`Fix Kitchen Sink`, not `fix kitchen sink`).
`chat_tools._title_case` normalizes new values on the way in, and
`_canonical_categorical` looks up any existing spelling for a categorical
field (via `db.find_existing_categorical` against the frozen
`_CATEGORICAL_FIELDS` set) so the agent reuses your existing category /
group names instead of coining a new casing variant.

---

## Game'N'Watch integration (Games & Shows)

Surfaces the [Game'N'Watch](https://github.com/Preston-Robertson/Game-N-Watch)
Discord bot's games/shows backlog inside this GUI. **The bot stores its data in
Google Sheets, not Postgres**, so this is a separate live read/write connection
to that spreadsheet — it does not touch `luigi_todo`.

**Data model** (`gnw.py`): reads/writes the bot's two worksheets, `Games` and
`Shows` (23 columns each). Columns are addressed by **header name** (row 1),
never by position, so the bot appending new columns never breaks the GUI. The
row key is `(Profile, Title)`, case-insensitive. Statuses: games =
`backlog / playing / paused / completed / achievements / dropped` (the
`achievements` column is labeled **100% Achievements**); shows =
`backlog / watching / on_hold / completed / dropped`. List reads are cached
~20 s and invalidated on write, keeping the boards snappy and well under the
Sheets API quota. When a status changes to *playing/watching* or *completed*,
the matching `Date Started` / `Date Completed` cell is stamped (only if empty),
mirroring the bot.

**Optional + graceful.** If `gspread`/`google-auth` aren't installed or the
sheet id / credentials aren't set, `gnw.disabled_reason()` drives a friendly
"not configured" panel instead of an error. `gspread==6.1.2` and
`google-auth==2.34.0` are in `requirements.txt`, so a restart (which re-runs
`pip install`) installs them automatically.

**Features in the GUI:**

* **Games** / **Shows** tabs — Kanban board bucketed by status, with a
  **profile selector** (All profiles, or one) that persists in the URL.
* **Add game/show** — search and select using the same source strategy as the
  Discord bot: Steam store search/direct app IDs for games; TVMaze + AniList
  and optional YouTube playlist search for shows. Metadata and cover art are
  written directly into the shared Google Sheet. Manual addition remains as a
  fallback when no result matches.
* Each card: cover art, external link (Steam / TVMaze / AniList / YouTube),
  priority, rating, platform/genre, tags, and (for shows) `S…·E…/total`
  progress. Game and show cards expose a prominent inline **Your rating**
  selector (`0–10`) that writes directly to the Sheet. A per-card **status
  menu** also writes straight to the sheet; an
  **Edit** button opens a modal for status, priority, rating, notes,
  platform, genre, tags, and (shows) season/episode/total.
* **🎲 Surprise me** — priority-weighted random pick (priority 5 is 5× as
  likely as priority 1), same algorithm as the bot's `/random`.
* Home widgets: **Currently Playing** and **Currently Watching**.
* **Steam stats** on Steam-backed cards reads owned-game playtime and personal
  achievement progress when a Steam Web API key + Steam ID64 are configured;
  refreshed playtime is also written to the existing `Hours Played` cell. It
  previews up to five locked achievements, reports 100% completion, and can
  move a fully completed game into **100% Achievements**. Steam requires the
  account's game details to be API-visible.

**Configuration.** Five environment keys, all editable from **Admin**:

| Var | Purpose |
|---|---|
| `LUIGI_WEB_GNW_SHEET_ID` | The sheet ID or full Google Sheets URL. Blank hides the tabs. |
| `LUIGI_WEB_GNW_CREDS_FILE` | Path to the service-account `credentials.json`. **Leave blank** to use the app-managed path `<repo>/gnw-credentials.json`. |
| `LUIGI_WEB_STEAM_API_KEY` | Optional Steam Web API key for personal playtime/achievements. Store search and metadata do not require it. |
| `LUIGI_WEB_STEAM_ID` | Steam ID64 whose owned games and achievements are displayed. |
| `LUIGI_WEB_YOUTUBE_API_KEY` | Optional YouTube Data API key for playlist results in Add Show. |

**Credentials without SSH.** The **Admin → "Game'N'Watch credentials"** panel
takes the service-account `credentials.json` as pasted text, validates it
(`type: service_account` + `client_email` / `private_key` / `project_id`),
writes it atomically at mode `600`, and hot-reloads the Sheets client — no host
file placement, no restart. It defaults to `<repo>/gnw-credentials.json`
because the systemd unit's `ProtectSystem=strict` + `ReadWritePaths=/opt/luigi-web`
make `/etc` read-only to the service, but `/opt/luigi-web` writable. That file
is git-ignored. Reuse the **bot's** service account (it already has Editor
access to the sheet); the panel reminds you to Share the sheet with the
account email if you ever use a new one.

