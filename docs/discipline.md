# Discipline

The authenticated `/discipline` workspace tracks habits against the shared
LuigiBot schema. The annual heatmap remains the main view; weekly progress and
organization controls complement it. See [../README.md](../README.md) for setup
and [architecture.md](architecture.md) for storage and module boundaries.

## Approved scope

| Decision | Current behavior |
|---|---|
| #1 Replace the annual heatmap | No. Keep the annual heatmap and year picker as the main view. |
| #2 Weekly target progress | Live for the current local week. |
| #3 Detailed history | Live Month/Year/Log detail through each habit's History link; the annual main view stays. |
| #4 Pause/resume | Live, preserving the habit and its history. |
| #5 Search, category filters, pins, order | Live; filters are temporary and pins/order are browser-local. |

UUID-keyed completion history, habit reminders, preferred weekdays, and
successful-week streaks are not implemented. The coordinated
[discipline-v2-plan.md](discipline-v2-plan.md) is future work with LuigiBot, not
a migration delivered by these changes.

## Annual history and weekly progress

Each habit retains the full selected year's 365 or 366 dated cells. The year
picker changes the annual history, not the current-week calculation. **Today**
returns to the current year or focuses today's cell when already viewing it.

Weekly progress uses the server's local date in `LUIGI_WEB_TIMEZONE`:

- The week runs Monday through Sunday, including weeks spanning two years.
- Only distinct completed dates from that Monday through today count. Future
  completions do not increase the count.
- Text shows the actual count and target, plus the remaining count or
  **Target met**. The bar stops at the target, but excess days stay visible in
  the count: four completed days against a three-day target displays `4 / 3`.
- The habit's current target is used only for the current-week calculation.
  Selecting an older history year does not apply that target to past weeks or
  reconstruct historical target changes.
- A **Daily streak** appears only for a target of seven days per week. It is
  the current streak computed from all available completion history, not a
  streak limited to the selected year.

Non-seven-day habits do not show daily-failure warnings on Discipline. This
does not change Review's existing `list_disciplines_at_risk()` heuristic; it is
not a global change to all habit summaries or risk calculations.

## Completion, pause, and delete

A habit keeps its stable definition and dated completion days. It does not
generate task-like copies after completion. Definitions have UUIDs, but legacy
`discipline_completions` still links days by task text, with one completion per
task/date. The annual heatmap first uses exact task text, then falls back to
case-insensitive, outer-whitespace-trimmed text when no exact days are found.
Weekly progress and today's state normalize case and whitespace, including
collapsing internal whitespace. These compatibility reads are not a UUID
history migration.

**Done today** marks or unmarks the server-selected local day. Live writes are
verified before the UI reports success; a failed mutation must not be presented
as a saved completion. Browser mutations require authentication and same-origin
CSRF protection.

**Pause** and **Resume** are in each habit's secondary actions menu:

- Pause uses the existing `POST /discipline/{uuid}/deactivate`; resume uses
  `POST /discipline/{uuid}/resume`.
- These operations update only `active`. The repository reads back the value
  in the transaction and commits before the route returns success.
- History and the habit definition remain intact. Paused habits cannot mark
  or unmark today; the server rejects that request with `409`. Past-date
  corrections remain available.
- The existing **Keep active** checkbox in the new/edit form controls the same
  active state: checked is active, unchecked is paused.

**Delete** is also in the secondary menu and remains destructive: it removes
the habit and its completion history. A 12-second Undo is available only when
the Tasks module is enabled. Without Tasks there is no Undo. Use Pause to keep
history rather than treating Delete as another form of pausing.

## Find and organize habits

The default filter is **Active**, with **Paused** and **All** also available.
Search matches the habit name and category. Category selection and weekly target
filters (**All targets**, **Remaining**, **Target met**) combine with search and
status. **Clear filters** restores the Active default. These selections are
page-memory-only, not saved views or cross-device preferences.

Pin controls put pinned habits before unpinned habits. Under **Organize habits**,
enable **Reorder habits** to move a habit up/down among its visible peers within
the same pinned or unpinned group. A move does not cross those fixed groups.
**Reset order and pins** restores the page's default order and clears pins.

Only layout metadata is stored in `localStorage` under
`luigi.discipline.layout`: version `1`, an `order` array, and a `pinned` array.
Each array is bounded to 500 unique habit UUIDs; pinned IDs must also be in the
order array. Names, categories, targets, and completion records are not stored
there. This is browser-local, not account-backed or synchronized across devices.

The controller checks storage writes by reading them back. Invalid or
unavailable saved layout falls back to the default order with a notice. If a
write cannot be confirmed, changes can still apply on the page, but an error
notice explicitly says they may not persist. It does not claim a successful
save. The same constraint applies when resetting the layout.

## Progress refresh and failures

The page initially renders current progress on the server. Authenticated
`GET /discipline/progress` returns `Cache-Control: no-store` JSON containing
the local date/week boundaries and UUID-associated statistics: active state,
weekly count/target/remaining/target-met, today's completion, and daily streak
where applicable. This response does not contain habit names.

After a verified completion update, `luigi:discipline-updated` triggers a
progress reload. Failed requests or invalid responses leave the last confirmed
progress values unchanged and display an unavailable notice with **Retry**.
They do not reset the counts to zero or imply a failed save succeeded. If the
initial weekly-progress calculation fails while annual history is available,
the heatmaps still render with progress marked unavailable, not zero. A failure
to load the underlying history itself is a separate availability error.

## Live detailed history

Each habit's **History** link opens live **Month**, **Year**, and **Log** views
for that habit. The main Discipline workspace remains the annual heatmap. All
three detail views use the same confirmed completion dates; changing the year
does not change the current-week summary or reconstruct historical targets.

The existing LuigiBot storage has a completion date (`completed_date`) and a
recorded timestamp (`logged_at`), not an actual completed-at time. Live history
shows **Logged at** when available, converted to `LUIGI_WEB_TIMEZONE`, and does
not offer a completion-time input. No schema migration, UUID-keyed completion
table, or historical target versions are introduced. Reads match task text
case-insensitively with outer whitespace trimmed; ambiguous habit identities
are rejected for writes.

The authenticated, `Cache-Control: no-store` endpoints are:

| Method | Path | Parameters |
|---|---|---|
| GET | `/discipline/{uuid}/history` | Optional `year`; renders the detail page. |
| GET | `/discipline/{uuid}/history/data` | Optional `year`; returns confirmed history and current-week state. |
| POST | `/discipline/{uuid}/history` | FormData `day`, `action` (`mark` or `unmark`), `expected_version`, `year`. |
| POST | `/discipline/{uuid}/history/undo` | FormData `token`, `year`. |

Selecting a day opens a correction dialog; opening it does not change history.
Dates must belong to the selected year and cannot be in the future. Paused
habits block today's changes, including Undo of today's change while paused,
but allow past-date corrections. Browser writes keep the normal authentication,
same-origin, and CSRF checks.

Writes compare a SHA-256 version of that day's raw rows before changing them
and verify the saved rows before returning success. Repeating a confirmed mark
does not replace its logged timestamp. A changed date offers **12-second Undo**,
using a bounded, process-local token tied to the habit and saved version. Undo
restores the original rows and their exact logged timestamps, not newly logged
copies; it cannot overwrite a newer change. This history Undo belongs to
Discipline and does not depend on the task-deletion Undo mechanism.

Conflicts, failed loads, and unconfirmed writes require a reload/retry to obtain
confirmed state before another change, never a blind repeat of a write. The
browser does not persist history records in `localStorage` or `sessionStorage`.
This feature adds no Finance, global search, or Assistant integration.

## History example

**History example** in the toolbar opens authenticated
`GET /discipline/history-preview`, also served with `Cache-Control: no-store`.
It uses fixed fictional data and a fixed sample clock, not live habit records.
It remains a separate comparison example, not the live per-habit History page
or evidence that the future history schema is installed.

- **Month**, **Year**, and **Log** show the same synthetic completion state.
- Selecting a date opens an explicit correction dialog; opening it alone does
  not toggle a completion.
- Corrections allow dates through the sample today, 24 April 2030. Future dates
  are blocked; a completion time on sample today cannot exceed the fixed
  sample time of 20:00 UTC.
- There is at most one completion per date. The dialog and log distinguish the
  exact completion date, completion time, and logged time in UTC.
- **Undo** reverses the last example change. **Reset example** or reloading the
  page restores the fixed sample data.

All example edits stay in browser memory. They are not stored in browser
persistent storage and do not write to backend records. The live year heatmap
and its data remain unchanged.

## Disposable preview

From the repository root in the development environment:

```powershell
python scripts/preview_workspace.py --check
python scripts/preview_workspace.py --discipline-demo --check
python scripts/preview_workspace.py --discipline-demo
python scripts/preview_workspace.py --discipline-demo --occurrence-demo
```

[../scripts/preview_workspace.py](../scripts/preview_workspace.py) selects an
available loopback port and prints the URL. It automatically authenticates its
port-scoped demo session, including direct links; do not enter production
credentials. It uses synthetic in-memory task/habit records, temporary
app-owned databases, and UTC for the demo clock. Real shared-database access,
external integrations, and deployment actions remain blocked.

Without `--discipline-demo`, the existing one-habit preview is preserved. The
flag provides four synthetic habits across multiple categories, with weekly
and daily targets and one paused habit. It can be combined with
`--occurrence-demo`; neither flag changes production configuration.

Allowed Discipline writes are bounded synthetic pause/resume, date toggles,
today's completion, and live history mark/remove/Undo, subject to the demo
session, same-origin, and normal CSRF checks. History mutation paths are
allow-listed exactly for the seeded habit UUIDs. The live detail pages share
the same in-memory completion rows as Today, the annual heatmap, and weekly
progress, including original synthetic logged timestamps on remove/Undo.
Preview corrections are bounded from January 1 of the previous year through
today; no real engine is used to simulate them. Habit creation, editing, and
deletion remain blocked. The separate history example still uses its own fixed
browser-memory data, not the helper's habit records.

`--check` checks 16 synthetic workspace endpoints, including `/discipline`,
`/discipline/progress`, and `/discipline/history-preview`. The new live history
paths are covered separately by
[../tests/test_discipline_history_preview.py](../tests/test_discipline_history_preview.py),
including default, `--discipline-demo`, and combined demo flags. `--check` is not
a production write test. Stop a running preview with Ctrl+C; its record storage is disposable.
Browser-local pins/order follow their usual browser storage behavior, separate
from the disposable records.

## Validation

Use a clean development environment with synthetic fixtures only, without
loading production credentials or local application records. Focused tests:

```powershell
python -m unittest discover -s tests -p "test_discipline*.py" -v
python -m unittest discover -s tests -p "test_discipline_history_preview.py" -v
```

The history preview tests run in cold subprocesses with `guard_preview_io`:
protected local files and external connections are blocked, only disposable
SQLite storage is permitted, and shared-engine access is asserted unused.

For full regression and repository validation, select all built-ins for the
validator so route coverage is not limited by a smaller module selection:

```powershell
python -m unittest discover -s tests -v
$env:LUIGI_WEB_MODULES = "tasks,discipline,planning,media,cards,characters,finance,assistant,admin,preview,feedback"
python scripts/validate_repo.py
git diff --check
```

The validator compiles shared-loader HTML templates and checks unique mounted
method/path registrations without startup hooks. Counts are not a fixed
documentation contract. For frontend changes, also review 1440x900 and 390x844
using synthetic previews only; never capture real habit or application records.