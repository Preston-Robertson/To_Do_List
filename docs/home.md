# Home and Assistant

## Live Home

The user-approved redesign is live at `/home`, not just a layout proposal.
It has one task list, Habits, Today's progress, Coming up, and static Continue
links, without duplicate summary tiles. The main lane contains Tasks and
Habits; the supporting lane contains the other three sections. On mobile,
the main lane comes first in a single page scroll.

The task list combines all nonarchived task and recurring-task rows, without
the old 25-row cap:

- **Today** shows open tasks explicitly selected for the current local day.
- **Upcoming** shows open tasks with a future due date that are not selected
   for Today.
- **All** shows all open tasks, with **Show completed** to include completed
   rows and reopen them.

Task creation, editing, completion/reopening, and habit completion use the
existing task and Discipline routes. Today's progress counts completed tasks
within today's selection; completing a selected task does not remove its
membership, even though it leaves the open Today list.

Coming up is an agenda derived from actual task due dates. This redesign does
not implement new Calendar events or scheduled focus blocks. Continue contains
static navigation links to enabled Games, Trading Cards, and Characters
modules. Home does not fetch their records, Finance records, or an Assistant
provider. It still belongs to Planning, which requires Tasks and Discipline;
this is not a new module-independent architecture.

## Today selections and writes

Today membership and due dates are independent. Adding or removing a task
from Today never changes its due date; rescheduling or clearing a date never
changes Today membership. Production starts with no selections, and nothing
is automatically selected merely because it is due or overdue.

Selections use the existing app-owned operations SQLite database configured
by `LUIGI_WEB_OPERATIONS_DB`. The added `home_today_selections` table stores
only references: `task_source` (`task` or `recurring`), `task_uuid`, and
`selected_date`. It does not copy task payloads. No LuigiBot migration,
schema-version bump, or destructive shared-table change is required.

The selection date comes from `clock.local_today()` in `LUIGI_WEB_TIMEZONE`.
Selections survive a restart for that date, but the next day does not silently
carry them forward. A Today submission from an older day's page receives
`409`; Home refreshes the day before another selection, or asks for a reload
if refresh fails. Task deletion snapshots include Today references, and the
existing Undo restores them with their original dates.

Authenticated `GET /home/data` returns current task, habit, and selection
state with `Cache-Control: no-store`. `POST /home/today` and
`POST /home/reschedule` require authentication and same-origin CSRF protection
for browser sessions. Today writes are read back after saving; date writes
are also read back and checked before success is reported. Rescheduling uses
the existing short-lived Undo mechanism. A failed or uncertain write is not
presented as a successful UI change.

If the selection store fails, Home explicitly reports that Today selection is
unavailable and disables selection controls while leaving tasks accessible.
It does not hide the entire task list or silently substitute due-date-based
selections. Broken task storage instead blocks Home with a generic `503`,
without database details or private records in the error.

## Global Assistant

When Assistant is selected, its toolbar button opens a side panel on desktop
and a full-width dialog on mobile. The panel loads only when opened. Home no
longer initializes the provider or displays chat before its heading.

The panel restores visible messages from the existing authenticated session;
system prompts, intermediate tool messages, and tool-call arguments are not
rendered. Closing it retains the current draft while staying on the same
page. Navigation reloads visible history from process memory, not browser
storage. Restarting the server clears that history. Opening it does not send
a message, scrape page content, or broaden the Assistant's allowed tools.
An unconfigured provider shows a disabled composer in the panel only.

## Customize layout

Home's **Customize layout** supports:

- showing or hiding each available widget;
- pinning widgets above unpinned widgets within the same lane;
- moving widgets up/down within their lane and pinned or unpinned group;
- restoring defaults in the draft;
- Save to commit, or Cancel/Escape to discard changes.

The main and supporting lanes are fixed: moving or pinning a section cannot
move it across lanes. The five visible sections reuse existing storage IDs:

| Section | Lane | Storage ID |
|---|---|---|
| Tasks | Main | `open-tasks` |
| Habits | Main | `disc-pending` |
| Today's progress | Supporting | `task-week` |
| Coming up | Supporting | `upcoming` |
| Continue | Supporting | `gnw-playing` |

Browser-local settings are the default. Previous hidden-widget preferences
are migrated when a versioned layout has not already been saved. All 13
previous widget IDs are still accepted and preserved in saved layouts, so
older order, visibility, and pin preferences are not discarded. Only the five
sections above render or appear in the editor. Retired widgets are absent;
preserving their IDs does not keep every old widget live or bring them back
when an integration is enabled.

To reset, open **Customize layout**, choose the desired storage scope, select
**Restore defaults**, then **Save**. This restores the default order, shows
all five sections, and clears pins. Reset changes only the draft until saved;
**Cancel** or Escape keeps the committed layout.

**Across devices** is an explicit opt-in on each browser. Selecting it loads
an existing shared layout when available; Save writes the chosen layout to
the host. New browsers must select that scope once. This is one shared layout
for the single-user application, not a multi-user account system. Updates
from another device load on the next Home visit, not through live polling.
Concurrent saves use last-successful-write behavior.

Shared settings contain only widget IDs, order, hidden IDs, and pinned IDs
under `LUIGI_WEB_DATA_DIR/home-layout.json` (or the normal data directory).
No task, Finance, card, character, or chat records are stored there. The file
is gitignored. Requests require authentication; browser writes require CSRF.
If a shared read fails, the browser retains its last layout and displays an
error. Saves show success only after confirmation; failures preserve the
previous committed layout. Switching back to browser-local does not delete
the host's shared copy.

Layout scope changes and saves use native async handlers with cleanup on
success and failure, preventing the earlier busy-state cleanup races.

## Comparison preview

The separate authenticated `/home/preview` page remains a synthetic,
browser-memory-only comparison demo. Task details, completion, Undo,
rescheduling, quick add, and Today membership are simulated there. The
approved unified list, two-lane layout, and compact empty states are now
implemented on live `/home`, not limited to this comparison page.

All example changes remain in page memory. Reload or **Reset demo** restores
the fixtures. No task writes or Today selections from this page are persisted.
Global record search and live HTMX actions are disabled inside the demo;
ordinary navigation links can leave it.

## Disposable workspace preview

[../scripts/preview_workspace.py](../scripts/preview_workspace.py) exercises
the actual Home UI using bounded synthetic task adapters. Task actions are
opt-in in the preview security hook; this helper explicitly enables them
after installing the in-memory adapters. It supports synthetic task
completion/reopening, creation, editing, dates, Today membership, habit
completion, and Undo. Task and habit records remain in memory; Today
references use a temporary operations SQLite store.

The helper preselects two synthetic tasks for screenshot/demo coverage only.
Production does not inherit that seed and starts with no Today selections.
Real shared `get_engine` access remains blocked, as do integration, Admin,
deployment, external refresh, and chat writes. Module selection, Home layout,
and Cards/Characters changes remain disposable. Assistant opens unconfigured
without contacting a provider.

The helper prints an available loopback URL and signs in automatically,
including direct links, using a port-scoped preview cookie. Production login
and its session are unchanged; do not enter real credentials. Run it in a
clean development environment and stop it with Ctrl+C when done:

```powershell
python scripts/preview_workspace.py
```

## Validation

Run focused Home and operations checks before the full offline suite, using
only a clean development environment and synthetic records:

```powershell
python -m unittest discover -s tests -p "test_home*.py" -v
python -m unittest discover -s tests -p "test_operations.py" -v
python -m unittest discover -s tests -v
python scripts/validate_repo.py
python scripts/preview_workspace.py --check
git diff --check
```

The helper's `--check` command checks 13 synthetic workspace endpoints,
including `/home/data`; it is not a production write or integration test.
Verify Today date scoping, restart persistence, stale-day rejection, deletion
and Undo, storage-failure behavior, authentication, and CSRF. Also check
preference compatibility, atomic save failures, Assistant lazy loading and
escaped history, and isolation of both preview modes. Host/example wheel
checks cover packaged templates and assets; report any skips.

Browser review at 1440x900 and 390x844 should cover task filters and Show
completed, completion/reopen, date changes and Undo, Today membership,
progress, habit writes, stable focus, lane-local pins/order, reset, shared
saves and outages. Check Assistant open/dismiss/focus and no provider request
before opening. On `/home/preview`, check reset and zero mutating demo
requests; in the disposable workspace helper, check the bounded synthetic
writes without enabling real storage or external services.