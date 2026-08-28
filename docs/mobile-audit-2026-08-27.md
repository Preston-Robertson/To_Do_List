# Mobile UX audit - 2026-08-27

## Scope

The app shell and authenticated routes were reviewed at 390x844 and checked
again at 1440x900. Browser checks covered login, Tasks, recurring tasks,
Archive, task rules, follow-ups, Discipline, Calendar, Activity, Reminders,
Games, Shows, Media Insights, Trading Cards, Finance unlock, Admin, Preview,
Feedback, and feedback export. Home, Projects, and Review were source-reviewed;
the local synthetic browser fixture returned 500 for those data-dependent
routes, while their offline route tests passed.

No Finance records were read for this audit. The unlocked Finance ledger and
populated Collection layouts were verified with temporary, fictional DOM rows
that were never submitted or persisted.

## Improvements completed

- Mobile navigation is fully off-canvas when closed, inert outside the active
  drawer, and restores focus when dismissed.
- Search and form drawers lock the obscured workspace, fit the phone viewport,
  and restore focus to their opener.
- Shared phone controls use 40-44px touch targets, including filters, modal
  close buttons, action menus, date presets, and destructive row actions.
- Phone layouts use the page as the single vertical scroll surface instead of
  nesting scrollable Kanban columns, widgets, and Discipline lists.
- Tasks keep independent phone and desktop view preferences and default to the
  board on phones.
- Calendar keeps independent phone and desktop preferences, defaults to Agenda
  on phones, and correctly hides the inactive Month panel.
- Discipline uses 40px day cells on phones, highlights and centers today, and
  resets to the compact year view on desktop.
- Follow-up rules, Finance transactions, and Trading Card collection entries
  become labeled two-column rows on phones while retaining table headers for
  assistive technology.
- Trading Cards navigation, filters, dialogs, inspector controls, and deck view
  preferences are phone-aware; deck stacks are the phone default.
- Projects keeps a narrower frozen task-name pane so more timeline remains
  visible on phones.
- Login uses the available phone width with consistent side gutters.

## Focused follow-up

1. Run a physical iOS Safari and Android Chrome pass for virtual-keyboard,
   safe-area, and momentum-scrolling behavior.
2. Consider a list/timeline switch for Projects. The Gantt chart remains an
   intentionally horizontal workspace even though its phone proportions are
   improved.
3. Consider month or quarter navigation for Discipline. The full-year heatmap
   remains horizontally scrollable by design, with today centered initially.
4. Recheck Home, Projects, and Review in a complete synthetic browser fixture
   so their populated states receive the same screenshot pass.
5. Recheck the unlocked Finance dashboard in a private local session. Current
   validation covers source structure, the unlock page, and synthetic geometry
   without accessing user records.

## Validation

- `python -m unittest discover -s tests -v`: 161 tests passed.
- 66 Jinja templates compiled through the configured app environment.
- 161 HTTP method/path registrations were unique.
- Workspace diagnostics and `git diff --check` were clean.
- Browser checks at 390x844 and 1440x900 found no page-level horizontal
  overflow on the reviewed routes.