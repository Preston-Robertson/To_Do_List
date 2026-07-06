# LuigiBot Web GUI (`luigi-web`)

Server-rendered FastAPI + Jinja2 web GUI for the LuigiBot to-do system. It is the
**second read-write client** of the shared Postgres database `luigi_todo`; LuigiBot
is the first. Both write concurrently — Postgres/MVCC keeps things safe.

> **Prerequisite:** The `luigi_todo` DB must be at `schema_version = 2` (the four
> list tables must have a `uuid` column). The app refuses to serve traffic if
> `schema_version < 2`.

---

## UI approach

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
All widget queries are read-only, bounded with `LIMIT`, and live in `db.py`
(`list_open_tasks`, `list_overdue_tasks`, `list_upcoming_tasks`,
`list_recent_completions`, `list_discipline_streaks`,
`list_disciplines_pending_today`, `list_disciplines_at_risk`,
`list_follow_ups_preview`, `weekly_discipline_counts`,
`weekly_task_completion_counts`, `list_recent_activity`, `weekly_review`).

**Kanban board by status.** Tasks and Recurring live on 3×2 Kanban boards. The
column layout is:

    Row 1 :  Not Started  |  In Progress  |  Completed
    Row 2 :  Blocked      |  Hiatus       |  Pending

The board is height-capped to the viewport — each column is an independent
scroll container, so a Completed column with hundreds of cards never blows up
the page. Drag a card between columns to change status. Click a card to edit
all fields in a modal. HTMX handles inline updates; SortableJS handles
drag-and-drop.

Each card has a **Snooze ▾** menu (+1d / +3d / +1w / +2w) that POSTs to
`/tasks/{uuid}/snooze` (or `/recurring/{uuid}/snooze`) and swaps the card in
place. Snooze math uses `max(today, current_due) + days`, so overdue items
always defer from today rather than from a stale due date.

**Undo toast.** Every complete / delete / snooze (on both `tasks` and
`recurring_tasks`) snapshots the pre-mutation row into a server-side in-memory
queue (`_UNDO_QUEUE` in `app.py`, capped at 64 entries with a 12 s TTL, guarded
by a `threading.Lock`) and emits an `HX-Trigger: showUndo` event carrying
`{op_id, label, ttl_ms}`. The client writes the pending entry to
`localStorage.luigi.pendingUndo` **synchronously in the same tick as the
`reloadBoard` trigger** so the toast survives the ensuing full-page reload;
`restoreUndoToast()` re-renders it on `DOMContentLoaded`. Clicking **Undo**
fires `POST /undo/{op_id}`, which pops the entry and calls
`db.restore_task_row(table, snapshot)` — one idempotent UPDATE-or-INSERT that
reverses complete, delete, and snooze uniformly (the row's original `uuid` is
reused, so any references remain valid). Expired ops return `410 Gone`.

**New-task auto-refresh.** After the New Task modal POSTs successfully, the
response carries an `HX-Trigger: reloadBoard` so the newly-created card shows
up in its column immediately without the user having to reload manually.

**Native date picker with presets.** The `Due date` field in the task modal
uses `<input type="date">` (so you get the OS-native calendar GUI — no
typing) plus quick chips: **Today · Tomorrow · +1w · +2w · Clear**. The
active preset stays highlighted. Wired in `initDatePickers()` in
`static/js/app.js`; re-initialized after every HTMX swap so modals opened
later still get the chips.

Above each board is a **filter bar** with free-text search, a status /
priority / category dropdown, and a **smart-list** picker with built-ins:
*Overdue*, *Due this week*, *No due date*, *High priority (≥ 5)*, and
*Completed this week*. Filtering is 100% client-side — the templates emit
`data-*` attributes on each card and `static/js/app.js` toggles visibility
in the DOM. Named filters can be saved ("☆ Save current") and reapplied per
endpoint; state lives in `localStorage` under
`luigi.tasks.savedFilters` and `luigi.tasks.activeFilter.<endpoint>`.

The DB-level status enum stays in its canonical order
(`db.STATUS_VALUES`); the display order is a separate constant
(`db.STATUS_DISPLAY_ORDER`) so reordering the board never changes what the
backend accepts.

Other views:

* **Projects** — category-scoped Gantt chart at `/projects`. Pick one or more
  `catagory` values (and optionally include recurring tasks); the page
  renders a two-pane view: a fixed names column on the left and a scrolling
  SVG timeline on the right. Bars derive their span from `start_time` (or
  `task_creation` as a fallback) through `due_date`; items without a
  `due_date` drop into an "Unscheduled" section below the chart. The
  header draws month gridlines/labels and a dashed "today" marker; bars are
  colored by status. Clicking a task name opens the same edit modal used
  on the Kanban.
  * *Planned:* a **task-flow / dependency web** view on the same tab —
    Azure ML Designer-style drag-and-drop nodes, but flowing **left → right**
    along a date axis instead of top-to-bottom. Each node is a task card
    (title, status chip, due date); edges are prerequisite links. Any task
    with an incomplete upstream node is auto-styled as **Blocked** (the
    Kanban status stays canonical; the flow view just visualises it).
    Timeline zoom + snap-to-day, click-drag to draw a dependency edge,
    click a node to open the existing edit modal. Requires one small
    schema addition (a `task_dependencies(uuid, blocks_uuid)` table) —
    LuigiBot's DDL side, not the GUI's.
* **Discipline** — one GitHub-style yearly heatmap per discipline item, with a
  year-picker dropdown. Click any day cell to mark/unmark.
* **Follow-ups** — plain table with inline edit.
* **Admin** — runtime info + self-update / restart controls + JSON backup
  export (see below).

### Future UI directions (noted for later)

* **Option A — dense Linear/Height-style table** with filter chips and a side
  drawer. Better once the task list grows and filtering matters more.
* **Option B — Todoist-style two-pane** with a "Today" landing page mixing
  tasks-due-today + disciplines-due-today.
* **Option C — Projects "task-flow" web (planned next).** Rework the
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

These are additive — the DB layer and route shape don't need to change to add
them; only new templates + route variants (and, for Option C, one new table).

---

## Data contract (short version)

Read `LUIGI_WEB_GUI_SPEC.md` and LuigiBot's `bot_modules/db.py` for the full
story. Hard rules the GUI must obey:

* Never insert an explicit `id` — PKs are `GENERATED ALWAYS AS IDENTITY`.
* `uuid` is the durable row handle. All `UPDATE`/`DELETE` are scoped `WHERE uuid = :uuid`.
* Booleans are `INTEGER 0/1` (`completed`, `recurring`, `active`).
* Dates and datetimes are ISO-8601 `TEXT` (`YYYY-MM-DD` or full ISO).
* Intentional SQL spellings: `catagory` (sic), `sub_group` in tasks, `subgroup`
  in follow-ups. The GUI talks SQL directly so it uses these names as-is.
* `discipline_completions` is append-only with `UNIQUE(task, completed_date)`.
  Mark = `INSERT ... ON CONFLICT DO NOTHING`. Unmark = `DELETE ... WHERE task
  AND completed_date`.
* No whole-table rewrites. Ever.
* No DDL from the GUI — LuigiBot owns the schema.

---

## Local dev

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# copy .env.example to .env and fill in the two secrets
Copy-Item .env.example .env

# smoke-test the DB connection (read-only)
python scripts\smoke_test.py

# run
$env:LUIGI_WEB_PG_HOST="10.0.0.202"    # etc. (or load .env with a dotenv loader)
uvicorn app:app --reload --host 0.0.0.0 --port 8080
```

Open `http://localhost:8080`. You'll be redirected to `/login`; enter the value
of `LUIGI_WEB_UI_TOKEN` to get a session cookie.

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
| `LUIGI_WEB_ENV_FILE` | Path the Admin env editor writes. Defaults to `<repo>/.env`; set to `/etc/luigi-web.env` on the LXC |
| `LUIGI_WEB_LLM_PROVIDER` | `openai` (default) or `disabled` |
| `LUIGI_WEB_LLM_BASE_URL` | OpenAI-compatible endpoint. Default `https://models.github.ai/inference` (GitHub Models) |
| `LUIGI_WEB_LLM_API_KEY` | Chat panel is disabled when blank. GitHub PAT with `models:read` for GitHub Models |
| `LUIGI_WEB_LLM_MODEL` | Default `openai/gpt-4o-mini` |
| `LUIGI_WEB_LLM_TIMEOUT` | HTTP timeout in seconds (default `60`) |
| `LUIGI_WEB_LLM_MAX_TOOL_ITERATIONS` | Cap on tool round-trips per message (default `5`) |

Secrets **must** live outside the repo — in `/etc/luigi-web.env` on the LXC
(mode `640 root:luigi-web`) or in `.env` locally. `.gitignore` blocks `.env`.

---

## Routes

Unauthenticated:
* `GET /healthz` → `{"status":"ok","schema_version":N}`
* `GET /login`, `POST /login`, `POST /logout`

Authenticated (session cookie, or `?token=` / `Authorization: Bearer`):
* `GET  /`                → redirects to `/home`
* `GET  /home`            → widget dashboard (overdue, upcoming, open tasks,
  discipline today, discipline streaks, follow-ups, recent completions,
  weekly discipline chart, weekly tasks-completed chart)
* `GET  /tasks`           → Kanban board
* `POST /tasks`           → create
* `GET  /tasks/{uuid}/edit`   → modal edit form (HTMX partial)
* `POST /tasks/{uuid}`    → update
* `POST /tasks/{uuid}/status` → drag-drop status change
* `POST /tasks/{uuid}/complete` → toggle `completed` + `completed_time`
* `POST /tasks/{uuid}/snooze` → defer `due_date` by `days` (form field);
  returns the re-rendered card partial for an HTMX swap
* `POST /tasks/{uuid}/delete` → delete
* `GET  /recurring` … (same shape as `/tasks`, including `/snooze`)
* `GET  /projects?catagory=X&catagory=Y&include_recurring=1` → Gantt chart
* `GET  /discipline?year=YYYY` → yearly heatmaps
* `POST /discipline`      → create
* `GET  /discipline/{uuid}/edit`, `POST /discipline/{uuid}` → update
* `POST /discipline/{uuid}/deactivate` → set `active=0`
* `POST /discipline/toggle` → mark/unmark a day (also used by the Home
  discipline widget's "Done" button)
* `GET  /follow-ups`      → table
* `POST /follow-ups`, `GET /follow-ups/{uuid}/edit`, `POST /follow-ups/{uuid}`,
  `POST /follow-ups/{uuid}/delete`
* `GET  /admin`           → runtime info + update / restart controls +
  backup export
* `GET  /admin/backup`    → read-only JSON dump of `tasks`,
  `recurring_tasks`, `follow_up_tasks`, `disciplines`, and
  `discipline_completions`. Served with
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
  which.
* `POST /chat`            → send one user message to the assistant; returns
  an HTML partial containing the user bubble, assistant reply, and any
  tool-call audit entries. Requires `LUIGI_WEB_LLM_API_KEY`.
* `POST /chat/reset`      → clear the in-memory chat history for the caller's
  session.
* `POST /undo/{op_id}`    → pop the queued snapshot for `op_id` and restore
  the row via `db.restore_task_row`. Returns `204` on success (with
  `HX-Trigger: {reloadBoard, undoCleared}`) or `410 Gone` if the op has
  expired or already been consumed. Covers complete / delete / snooze on
  both `tasks` and `recurring_tasks`.

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

---

## Self-update

The `/admin` page exposes two buttons backed by the routes above:

* **Update** — runs `git fetch`, `git pull --ff-only`, then
  `pip install --no-cache-dir -r requirements.txt` in `/opt/luigi-web`. Streams
  each step's stdout/stderr and exit code back into the page so a failed pull
  (non-fast-forward, dirty tree, network error) is immediately visible.
* **Restart** — sleeps 0.6 s, then calls `os._exit(0)`. `systemd` relaunches
  the service because the unit has `Restart=always`.

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
* Rotate the shared token by editing `/etc/luigi-web.env` and restarting.

---

## Non-goals

* No DDL from the GUI.
* No optimistic concurrency — last-write-wins scoped by `uuid` (future work).
* No user accounts — single shared token. Rotation = change env + restart.
* No charts/analytics parity with the bot beyond the two weekly bar charts on
  the Home dashboard and the category-scoped Gantt on `/projects`.
* No changes to LuigiBot.

---

## Assistant (LLM chat panel)

A collapsible chat panel at the top of `/home` lets you drive the app in
natural language: *"add task fix printer priority 3 due tomorrow"*, *"mark
read discipline done"*, *"what's overdue?"*. It's disabled by default; set
`LUIGI_WEB_LLM_API_KEY` and restart to enable.

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
