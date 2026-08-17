# Screenshot capture guide

This directory is reserved for public screenshots embedded in the repository
README. Capture the final responsive shell with a temporary, fully synthetic
dataset created only for screenshots. Never capture real Finance or personal
task data.

## Required files

| File | Page/state | Recommended viewport |
|---|---|---|
| `home-dashboard.png` | Home with the Focus widgets and sidebar visible | 1440 × 900 |
| `tasks-board.png` | Tasks in Board view, with representative one-off and recurring cards | 1440 × 900 |
| `tasks-list.png` | Tasks in List view with an overflow menu open | 1440 × 900 |
| `calendar.png` | Calendar on a month containing several due dates | 1440 × 900 |
| `games.png` | Games board with cover art and an inline rating visible | 1440 × 900 |
| `finance.png` | Finance dashboard with synthetic aliases, balances, budget, and alert | 1440 × 900 |
| `command-palette.png` | `Ctrl+K` open with grouped search results | 1440 × 900 |
| `mobile-tasks.png` | Tasks on mobile with the navigation drawer closed | 390 × 844 |

## Capture checklist

1. Use the dark theme and a temporary screenshot-only database.
2. Populate every visible page with invented labels and invented values only.
3. Never show legal names, addresses, emails, phone numbers, account/card/
   routing numbers, tax identifiers, credentials, tokens, private paths, host
   addresses, real transaction descriptions, or real balances.
4. Keep browser zoom at 100% and hide browser developer tools.
5. Capture PNG files at the exact names above—README markup is already staged
   in an HTML comment and can be uncommented once the files exist.
6. Prefer lossless optimization (`oxipng` or equivalent) without resizing.
7. Delete the screenshot-only Finance database after capture and inspect each
   image at full resolution before publishing.

The application intentionally uses stable page headers, persistent sidebar
width, drawers, and deterministic Board/List layouts so captures remain aligned
and comparable across releases.
