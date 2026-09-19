# Games and Shows

The library is available at `/games` and `/shows`. Current item fields still
belong to the shared Game'N'Watch Google Sheet. Local web activity and replay
or rewatch runs have a separate Luigi Web history store; they are not a copy
of the bot's History or Sessions worksheets.

## 1. Library views and saved preferences

**Continue** is the default view for a browser/profile without a saved
preference. **Board** and **List** remain available. Item details keep editing,
activity, and run history together; catalog search and manual addition remain
available through the existing add dialog.

Selected view, filters, sort order, recent-pick exclusion, and named saved views
are browser-local preferences, scoped to section and profile. The all-profiles
scope has its own preferences. These preferences do not synchronize across
devices and do not store item records, notes, activity, or run history. Filter
text and saved-view names are preferences, so they can still contain text the
user enters. Failed browser storage must not be mistaken for a saved record.

## 2. Filters and picker

Search, profile, status, platform, genre, tags, priority, and rating narrow the
library. Sorting supports recent activity, title, priority, rating, newest
added, and oldest added. The picker chooses from the filtered candidate set,
using the existing priority/backlog-health weighting.

Recent-pick exclusion uses a bounded, process-local record of picks from the
last 24 hours. It is not durable watch/play history, does not infer progress,
and resets when the worker restarts. An empty eligible pool returns no item;
it does not silently discard the filters. The exclusion preference is saved
in the browser, but the recent-pick records themselves are not.

## 3. Show progress

Shows expose **+1 episode** and direct season/episode editing. A quick episode
change is confirmed against the Sheet before replacing the displayed item.
Season transitions, episode resets, total-episode corrections, and completion
are manual choices. Reaching a counter value does not automatically advance
the season or mark a show completed. Unknown totals remain unknown.

Completed or dropped shows need an explicit status change or **Start rewatch**
before another quick episode increment. A rewatch starts at season 1, episode
0 while preserving prior runs and the original Sheet date columns.

## 4. Explicit Steam snapshots

Steam-backed games have separate display, refresh, and save actions:

- `GET /media/games/steam` reads the process-local Steam snapshot cache. A
  cache miss does not call Steam. Resolving the current Sheet item can still
  require a Sheet read.
- `POST /media/games/steam/refresh` explicitly fetches a new snapshot. Opening
  details, rendering the library, or reading the cache does not refresh Steam.
- `POST /media/games/steam/save` saves validated playtime from the selected
  snapshot through the normal version-checked library mutation. Refresh alone
  does not save playtime to the Sheet.

Snapshots are bound to profile, title, Steam app ID, and the current Steam
configuration identity. The opaque snapshot ID must match the cached entry;
changing configuration or refreshing the snapshot invalidates the old binding
or ID. Secrets and configuration values are not returned to the browser.

The display TTL is **300 seconds**. Older snapshots may remain visible as
stale, but reading them does not renew their age. Save eligibility is separate:
the matching snapshot must be **less than 600 seconds old** and contain known,
valid playtime. Snapshot storage is bounded and disappears on worker restart.

Private, unavailable, absent, or malformed playtime and achievement data are
unknown, not zero. A known zero is distinct from unavailable data. Achievement
progress is informational: it never automatically changes an item's status
to completed or achievements. Playtime saves are explicit, not background sync.

## 5. Runs, activity, and Undo

**Start replay** and **Start rewatch** explicitly create a new local run for a
finished or dropped item and return the current Sheet row to the active status.
Earlier runs remain intact. The shared Sheet's `Date Started` and
`Date Completed` retain their original first-ever dates; new run timestamps
belong to local web history. Legacy snapshots preserve the available baseline,
not a reconstructed sequence of earlier plays or watches.

History is **web-tracked only**. Missing bot actions, external Sheet edits, and
past sessions are not reconstructed. Identity is the normalized section,
profile, and title, not a stable provider-independent item UUID. History cannot
yet follow a title or profile rename; a changed identity may have a separate
history. Do not interpret missing history as proof of no previous activity.

Successful eligible writes offer **12-second Undo**. Its bounded token and
expiry are process-local. Undo verifies that the item and local history have
not moved on, then restores the captured Sheet cells and local run state. It
does not erase an intervening edit. Expired tokens, a worker restart, or newer
state reject Undo. The same cross-store confirmation limits described below
also apply to Undo.

## 6. Insights

`/media/insights` retains the current status/rating/platform-or-genre charts,
accessible data tables, highly rated unfinished items, and backlog health.
**Recorded web activity** adds the selected section/profile's confirmed
last-30-day completed runs and changes, plus hours added for games or episodes
added for shows. It also separates web-recorded completion totals, the legacy
completed baseline, and operations still awaiting confirmation.

The `history.insights(section, profile)` context contract is:

| Field | Meaning |
|---|---|
| `completed_runs` | Retained completed runs, including the legacy baseline |
| `recorded_completed_runs` | Retained completed runs recorded by the web |
| `legacy_completed_runs` | Completed legacy snapshots, not a timed trend |
| `confirmed_changes` | Confirmed web changes excluding undone events and Undo itself |
| `pending_count` | Non-confirmed operations, including retained unconfirmed attempts |
| `last_30_days.completed_runs` | Web-recorded completions in the recent window, excluding legacy snapshots |
| `last_30_days.confirmed_changes` | Confirmed, non-undone changes in that window |
| `last_30_days.hours_added` | Positive changes between known hour counters, excluding replay resets |
| `last_30_days.episodes_added` | Positive changes between known episode counters within the same season, excluding replay resets |

These are recorded counter changes, not measured play/watch sessions. A manual
correction or explicit Steam save can contribute to a counter delta. Unknown
starting values do not become zero, and season transitions do not imply a
number of watched episodes. Lifetime totals divided by item age are not
velocity. Legacy dates and snapshots never become fabricated recent activity
or a completion trend. An unavailable history store renders an unavailable
state, not zero activity; current Sheet-based insights remain independent.

## 7. Refresh and confirmed updates

Sheet reads use a **20-second** section cache and per-section single-flight
coordination. Library data includes an update timestamp and cache state.
Explicit **Refresh** requests a fresh Sheet read. Relevant successful writes
replace cached reads with verified fresh data or invalidate them. Failed writes
invalidate the section cache before recovery.

Changes validate editable fields and the submitted item version against a
fresh row. Related cell updates use a verified Sheet batch, preserving
unrelated cells. The browser replaces confirmed item content without a full
page reload. Rejected or uncertain writes do not optimistically advance
progress or silently retry. Reload confirmed state, then explicitly retry if
needed. Generic errors must not expose provider payloads, credentials, or
filesystem paths.

Version checks and local locks reduce stale writes within the one web worker;
they are **not distributed optimistic concurrency with Google Sheets**. There
is no atomic compare-and-swap protecting the interval between a Sheet read and
write against the bot or another external client.

### Cross-store journal and recovery

The operation journal stores the before-state and intended `expected_fields`
before the Sheet mutation. Only a verified result confirms the local event and
run changes. SQLite transactions protect local history, but **the Sheet write
and SQLite commit are not atomic together**. A Sheet write can succeed while
history confirmation fails; the response must retain that uncertainty.

Reconciliation runs against a fresh authoritative item under the local
mutation lock, with no active write for that item:

- Intended fields match and represent a real change: confirm the pending
  operation using the verified state.
- Intended fields remain unchanged: retain the attempt as unconfirmed without
  inventing an event.
- Partial or conflicting results: retain an unconfirmed attempt and a warning;
  do not manufacture the intended event or run. Older attempts lacking the
  expected-field contract can remain unresolved when the result is ambiguous.
- A storage or confirmation failure leaves uncertainty visible and can keep
  the reservation blocked until a later fresh read can resolve it.

Recovery records the web confirmation time, not a reconstructed external
write time. Refresh cannot establish who changed a matching Sheet value, nor
can it supply missing bot or external activity.

## Storage boundary

`LUIGI_WEB_MEDIA_DB` selects the isolated app-owned SQLite history file. Its
default is `DATA_DIR/media.sqlite3`; `DATA_DIR` follows `LUIGI_WEB_DATA_DIR`, the
source-checkout data directory, or the installed per-user default. Use a
dedicated writable path outside version control, separate from every other
application data domain.

First initialization requires a new or empty database. Media stamps its own
SQLite `application_id` (`0x4C574D48`); an existing Media-owned database may be
reopened, while a nonempty unowned database or another application's ID is
rejected. Never point this setting at another feature's database.

The store owns `media_subjects`, `media_runs`, `media_operations`, and
`media_events`. It neither modifies LuigiBot's schema/version nor writes the
bot's History or Sessions worksheets. It does not add private records to chat
tools or global search. Keep the SQLite file and journal/WAL companions out
of commits, fixtures, screenshots, and public artifacts. Back up this store
separately; a Sheet-only backup does not contain web run/activity history.

## HTTP surface

All library routes require the main application authentication. Mutating
browser requests retain the host's same-origin CSRF protection; bearer-token
clients retain the existing cookie-independent API contract. Library JSON
responses use `Cache-Control: no-store`.

| Method | Route | Purpose |
|---|---|---|
| GET | `/games`, `/shows` | Library entry points |
| GET | `/media/insights` | Current Sheet summaries and recorded web activity |
| GET | `/media/{section}/data` | Current library state |
| POST | `/media/{section}/refresh` | Fresh Sheet read and pending-operation reconciliation |
| POST | `/media/{section}/change` | Version-checked editable fields |
| POST | `/media/{section}/episode` | Explicit show episode increment |
| POST | `/media/{section}/runs` | Explicit replay or rewatch |
| POST | `/media/{section}/undo` | Expiring, version-checked Undo |
| GET | `/media/{section}/detail` | Fresh item plus local activity and runs |
| POST | `/media/{section}/pick` | Filtered weighted pick |
| GET | `/media/games/steam` | Cached Steam snapshot only |
| POST | `/media/games/steam/refresh` | Explicit Steam provider refresh |
| POST | `/media/games/steam/save` | Save validated snapshot playtime |

`section` is `games` or `shows`. Existing `/gnw/` add, search, and compatibility
routes remain; they are not alternate stores of web history.

## Synthetic validation

Run the focused media suite in a development environment without production
credentials or live data. Fixtures must remain synthetic, with temporary
SQLite stores that are closed and removed after use:

```powershell
python -m unittest discover -s tests -p "test_media*.py" -v
python scripts/preview_media.py --check
git diff --check
```

The dedicated synthetic preview uses loopback port `58107` by default:

```powershell
python scripts/preview_media.py
```

Check the library and Insights at `1440x900` and `390x844`, including unavailable
history, unknown Steam values, filtered empty states, rejected writes, and
explicit recovery. Use an unused loopback port if another process owns the
default. These are validation commands and acceptance checks, not a claim
that a particular checkout has passed them. Broader repository validation
remains documented in [architecture.md](architecture.md#validation).