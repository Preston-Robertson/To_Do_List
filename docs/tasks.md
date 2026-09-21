# Tasks

Tasks combines one-off and recurring work at `/tasks`, using the existing
LuigiBot-owned tables. This iteration keeps the Board default and existing
statuses. It does not change the approved Home or on-demand Assistant behavior;
see [home.md](home.md).

## Approved scope

| Proposal | Current scope |
|---|---|
| #1 List-first layout and compact mobile rows | Example only at `/tasks/preview`; awaiting approval |
| #2 Tasks Add/Remove Today, including bulk selection | Not included; no Tasks Today-membership actions were added |
| #3 Simplified creation | Live: name-only quick capture and one Add task form with Repeat |
| #4 Saved views | Live: versioned browser-local view settings |
| #5 Stable Board | Live: fixed status-column order, visibility, and collapse controls |
| #6 Automation consolidation | Example only at `/tasks/preview`; awaiting approval |

The live **Completion triggers**, **Task rules**, and **Archived** links remain
separate. Existing completion-trigger, dependency, reminder, and archive
behavior has not been consolidated into a new Automation page.

## Creation and editing

Quick capture asks only for a task name and creates a one-off task. **Add task**
opens one form with these basics: task name, status, due date, project, and
integer priority from 0 to 10. The initially collapsed **Details** section holds
category, group, subgroup, link, and estimated hours. The shared schema's
`catagory` spelling is retained internally.

For new tasks, **Repeat** starts off. Enabling it exposes an interval after
completion, selected weekdays, or a monthly position. Inactive schedule fields
are disabled; repeated `recurring_days` form values are preserved so selecting
multiple weekdays does not silently keep only the last one.

Authenticated `POST /tasks` makes the source decision on the server. Repeat off
uses `create_task` and discards recurrence fields; Repeat on validates the
schedule and uses `create_recurring`. The handler reads the created row back
from the matching repository path and verifies its UUID before returning a
success response. It does not convert existing rows between `tasks` and
`recurring_tasks` or introduce a new shared task table.

The existing `/recurring/new` entry point remains source-compatible: Repeat is
locked on for a new recurring row and the form still posts to `/recurring`.
Editing a recurring row retains its **Repeat active** flag and schedule fields;
disabling recurrence does not move it into the one-off table. Existing one-off
edit forms do not offer a conversion toggle.

Failed or unconfirmed editor saves keep the form and draft visible with an
error, rather than closing the modal or displaying success. The Tasks editor
checks the response's success trigger, source, and card identity before closing
and refreshing. After an uncertain result, check the task list before retrying.
The editor assets load through the shared shell whenever Tasks is enabled, so
the same form works in existing Home modals; successful Home responses remain
owned by Home's existing handler.

## Scheduled recurrence

Copy-per-occurrence scheduling is implemented but **not automatically enabled
in production**. `LUIGI_WEB_RECURRENCE_OWNER=external` is the default: no web
scheduled generation and no old web reset. Existing web-only installations
must explicitly opt into `web` through deployment configuration to continue
automatic recurrence. First disable/co-ordinate LuigiBot's legacy reset
scheduler; Luigi Web does not detect or disable it. External ownership leaves
handling external and does not protect history from a bot that still resets
rows. There is no GUI ownership toggle.

With web ownership, startup and existing task/Home read paths create one due
successor in `recurring_tasks`. The completed parent retains its ID, completion
timestamp, status, schedule, and other fields. The child copies the definition
and has a new UUID, creation time, and calculated due date, with status
`Not Started`, no completion/start time, and zero logged hours. A delayed sweep
can create an overdue child; it does not backfill every missed date. Only that
child's later completion can lead to the next occurrence.

The current occurrence carries the rule; there is no separate template table.
Edit the current occurrence to change its next successor. Before a successor
exists, a mistaken completion can still be corrected or undone. After generation,
the parent's history is locked against editing, reopening, snoozing, or Undo to
an incomplete state, even if ownership returns to `external`. Explicit archive
and delete remain available; deleting a child does not make it respawn.

See [recurring-occurrences.md](recurring-occurrences.md) for schedule/date rules,
adoption limits, ledger permissions, and the required backup of both recurring
rows and lineage. Calendar projections are not persisted occurrences.

## Board and saved views

Board is the first-visit default when there is no remembered mode. The existing
status columns retain their canonical order, including when empty or after
filtering; they do not automatically move to the end or flip order. Each column
can be hidden or collapsed, with at least one column kept visible. **Collapse
completed** controls the Completed column. Both visibility and collapse state
are included in a saved view, without changing any task's status.

**Saved views** captures the complete presentation state:

| Setting | Values |
|---|---|
| Filters | Text query, project, category, status, one-off/recurring source, minimum priority, smart filter |
| Sort | Default order, due date, title, priority |
| List grouping | None, project, status |
| Mode | Board or List |
| Density | Comfortable or compact |
| Board columns | Visible columns and collapsed columns, including Completed |

Smart filters include open, overdue, upcoming, due this week, no due date, high
priority, completed this week, awaiting reactivation, recurring, and completed.
Date comparisons use `data-task-calendar-date` on the page root, supplied by
the server's configured local date when the page loads. An open page does not
automatically advance that anchor at midnight; reload to refresh it. These are
filters, not daily-selection actions. Project choices and group headings use
the existing lowercased row datasets; original label casing is not guaranteed.

Up to 30 named views can be saved per scope, with names of 1 to 40 characters.
Views can be applied, updated, renamed, or deleted. **Clear** clears filters;
**Reset view** restores the default presentation without deleting named views.
Changes to the current view are also remembered when browser storage works.

### Storage and compatibility

The versioned store is `localStorage["luigi.tasks.views.v1.<scope>"]`, where
`<scope>` is the page's endpoint scope, such as `/tasks`. It contains the current
view, named views, and active view name. It is local to that browser origin and
profile, not a physical user account, and has no cross-device synchronization.
Filter text and view names are browser-local settings; task records are not
copied into the saved-view store.

When a scope has no new store, valid legacy `luigi.tasks.savedFilters` and
`luigi.tasks.activeFilter.<scope>` values are migrated. The old remembered
Board/List mode seeds the new endpoint store; subsequent visits use that
store's own current mode. Legacy keys are retained, not deleted by migration
or by later deletion of a new-format view.

Invalid new-store data falls back to defaults with a visible notice and is not
overwritten merely by loading the page. Storage failures leave current view
changes page-only with a notice; named-view writes require readback confirmation
before reporting success. A failed named-view write keeps the existing named
views in that page unchanged, rather than claiming persistence.

The view controller identifies records with source-qualified keys such as
`task:<uuid>` and `recurring:<uuid>`. This avoids conflating sources during
filtering and ordering; it is not a new database identity or table migration.

## Examples awaiting approval

Open **Tasks > Examples** for authenticated, no-store `GET /tasks/preview`.
Both proposals remain separate from the live Tasks workspace:

- **#1 List workspace:** list-first presentation with compact mobile task-row
  controls, quick capture, filtering, sorting, editing, completion/reopening,
  and deletion, all simulated.
- **#6 Automation:** Completion triggers, Dependencies, and Reminders tabs with
  simulated create, edit, enable/disable, and delete controls. Completion rules
  generate a new copy instead of resetting an existing task.

The page is seeded entirely from fictional literals, with no SQL or live
record reads. Changes stay in browser memory: no mutating network calls or
browser record storage. **Undo** restores the last simulated change; reload or
**Reset examples** restores the fixtures. Ordinary navigation can leave the
example. The existing `/home/preview` comparison remains unchanged.

### Task-generation requirement

Automatic rules must create a fresh task instance from the configured task
type or template. A completed task remains completed with its original ID and
completion timestamp; it is never reused as the next occurrence. Each new
instance has its own ID, starts incomplete, and retains its task-type/rule
association so its completion can generate another instance of the same type.
This is a copy-and-create model, not an automatic reopen model. Explicit manual
correction of a mistaken completion and Undo are separate actions.

The Automation example now demonstrates this behavior. It permits a completion
rule to create a fresh instance of its own type without recursively completing
that instance. The list displays completion timestamps; copied instances do
not inherit a completed state or completion timestamp. An already-completed
template keeps its history, and Undo removes only the changes from the last
simulated action. All of this example state still resets on page reload.

Current live follow-up rules already insert new one-off task rows with fresh
UUIDs, rather than reopening existing tasks. Scheduled recurrence now also
supports retained-history copies through the deployment opt-in described above.
This backend implementation does not approve or activate the proposed Automation
page, and the example does not change production scheduler ownership.

## Disposable workspace preview

The separate [../scripts/preview_workspace.py](../scripts/preview_workspace.py)
helper exercises actual controllers with bounded synthetic adapters, not the
examples' simulated forms:

```powershell
python scripts/preview_workspace.py --check
python scripts/preview_workspace.py
```

Use the dynamically selected loopback URL printed by the helper. Demo login is
automatic, including direct links; do not enter real credentials. The
port-scoped preview session does not replace the normal application session or
change production authentication.

The helper seeds an example recurring task and supports one-off quick capture,
creation, editing, status and completion changes, plus recurring creation,
editing, status, completion/reopening, and completion Undo. Record mutations
are limited to exact synthetic IDs held by the adapters and allow-listed
routes, with a valid demo session and normal CSRF checks. Task records stay in
memory; supporting operations state uses temporary storage. Existing bounded
Home demo actions remain available.

Real shared storage, external refreshes, integrations, provider calls, Admin,
deployment, and chat writes remain blocked. `--check` checks 14 synthetic
workspace endpoints, including `/tasks/preview` and `/home/data`; it is an
endpoint smoke check, not production write or full-suite verification. Stopping
the helper discards its temporary application data. The live Tasks saved-view
preferences still use browser storage, unlike the memory-only examples.

## Implementation and validation

The implementation retains Python, FastAPI, Jinja, HTMX, and locally served
assets, with no new language, framework, CDN, or dependency requirement.
Relevant ownership is in
[../module-repos/tasks/src/luigi_web/modules/tasks/routes.py](../module-repos/tasks/src/luigi_web/modules/tasks/routes.py),
[../module-repos/tasks/src/luigi_web/modules/tasks/templates/partials/task_form.html](../module-repos/tasks/src/luigi_web/modules/tasks/templates/partials/task_form.html),
[../module-repos/tasks/src/luigi_web/modules/tasks/static/task-editor.js](../module-repos/tasks/src/luigi_web/modules/tasks/static/task-editor.js),
[../module-repos/tasks/src/luigi_web/modules/tasks/static/task-views.js](../module-repos/tasks/src/luigi_web/modules/tasks/static/task-views.js),
and [../module-repos/tasks/src/luigi_web/modules/tasks/examples.py](../module-repos/tasks/src/luigi_web/modules/tasks/examples.py).

From the repository root, in a clean development environment without production
credentials, the focused and full regression entry points are:

```powershell
python -m unittest discover -s tests -p "test_task*.py" -v
python -m unittest discover -s tests -p "test_tasks_preview_integration.py" -v
python -m unittest discover -s tests -p "test_occurrence_scheduler.py" -v
python -m unittest discover -s tests -p "test_recurring_occurrences.py" -v
python -m unittest discover -s tests -v
python scripts/validate_repo.py
git diff --check
```

The first pattern includes the separately runnable
[../tests/test_tasks_preview_integration.py](../tests/test_tasks_preview_integration.py)
suite. The repository validator compiles templates and checks mounted route
uniqueness without startup hooks; select all built-ins for full host route
coverage. See [../README.md](../README.md#development) for packaging prerequisites
and synthetic-test constraints. Frontend changes additionally need browser
checks at 1440x900 and 390x844; command completion alone is not visual validation.

The saved-view helper tests use Node when available. Without a Node executable,
that runtime test class is explicitly skipped; the Python/template tests do
not establish JavaScript behavior by themselves. Browser checks should cover
creation with Repeat off/on, rejected saves, saved-view reload persistence,
column visibility/collapse, grouping, and status rollback. The examples must
remain synthetic and separate from live task or automation writes.
