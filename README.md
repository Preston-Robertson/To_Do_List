# LuigiBot Web GUI (`luigi-web`)

Server-rendered FastAPI + Jinja2 web GUI for the LuigiBot to-do system. It is the
**second read-write client** of the shared Postgres database `luigi_todo`; LuigiBot
is the first. Both write concurrently — Postgres/MVCC keeps things safe.

> **Prerequisite:** The `luigi_todo` DB must be at `schema_version = 2` (the four
> list tables must have a `uuid` column). The app refuses to serve traffic if
> `schema_version < 2`.

---

## UI approach — v1

**Kanban board by status** (Option C from the design discussion). Tasks and
Recurring live on Kanban boards whose columns are the six fixed statuses:

    Not Started · In Progress · Pending · Blocked · Hiatus · Completed

Drag a card between columns to change status. Click a card to edit all fields in
a modal. HTMX handles inline updates; SortableJS handles drag-and-drop.

Other views:

* **Discipline** — one GitHub-style yearly heatmap per discipline item, with a
  year-picker dropdown. Click any day cell to mark/unmark.
* **Follow-ups** — plain table with inline edit.

### Future UI directions (noted for later)

* **Option A — dense Linear/Height-style table** with filter chips and a side
  drawer. Better once the task list grows and filtering matters more.
* **Option B — Todoist-style two-pane** with a "Today" landing page mixing
  tasks-due-today + disciplines-due-today.

These are additive — the DB layer and route shape don't need to change to add
them; only new templates + route variants.

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

Secrets **must** live outside the repo — in `/etc/luigi-web.env` on the LXC
(mode `640 root:luigi-web`) or in `.env` locally. `.gitignore` blocks `.env`.

---

## Routes

Unauthenticated:
* `GET /healthz` → `{"status":"ok","schema_version":N}`
* `GET /login`, `POST /login`, `POST /logout`

Authenticated (session cookie, or `?token=` / `Authorization: Bearer`):
* `GET  /`                → redirects to `/tasks`
* `GET  /tasks`           → Kanban board
* `POST /tasks`           → create
* `GET  /tasks/{uuid}/edit`   → modal edit form (HTMX partial)
* `POST /tasks/{uuid}`    → update
* `POST /tasks/{uuid}/status` → drag-drop status change
* `POST /tasks/{uuid}/complete` → toggle `completed` + `completed_time`
* `POST /tasks/{uuid}/delete` → delete
* `GET  /recurring` … (same shape as `/tasks`)
* `GET  /discipline?year=YYYY` → yearly heatmaps
* `POST /discipline`      → create
* `GET  /discipline/{uuid}/edit`, `POST /discipline/{uuid}` → update
* `POST /discipline/{uuid}/deactivate` → set `active=0`
* `POST /discipline/toggle` → mark/unmark a day
* `GET  /follow-ups`      → table
* `POST /follow-ups`, `GET /follow-ups/{uuid}/edit`, `POST /follow-ups/{uuid}`,
  `POST /follow-ups/{uuid}/delete`

---

## Deployment (CT 105 @ 10.0.0.203)

See `luigi-web.service`. Summary:

1. Unprivileged Debian 12 LXC, `onboot=1`, static IP `10.0.0.203/24`.
2. `apt install python3-venv git` · create user `luigi-web` · clone repo into
   `/opt/luigi-web` · build venv · `pip install -r requirements.txt`.
3. `/etc/luigi-web.env` (mode `640 root:luigi-web`) holding
   `LUIGI_WEB_PG_PASSWORD` and `LUIGI_WEB_UI_TOKEN`.
4. `cp luigi-web.service /etc/systemd/system/` · `systemctl enable --now luigi-web`.
5. UFW: `ufw allow from 10.0.0.0/24 to any port 8080 proto tcp`; default deny.
6. Confirm: `psql -h 10.0.0.202 -U luigi_web -d luigi_todo -c "SELECT 1;"`.

No NFS. This app runs standalone (its own LXC/systemd), independent of the Bot
Manager.

---

## Non-goals (v1)

* No DDL from the GUI.
* No optimistic concurrency — last-write-wins scoped by `uuid` (future work).
* No user accounts — single shared token. Rotation = change env + restart.
* No charts/analytics parity with the bot.
* No changes to LuigiBot.
