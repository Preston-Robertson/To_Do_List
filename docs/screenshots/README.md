# Screenshot capture guide

This directory is reserved for the production screenshots embedded in the
repository README. Capture after the redesigned build is deployed so images
show real data, final fonts, and the actual responsive shell rather than mock
content.

## Required files

| File | Page/state | Recommended viewport |
|---|---|---|
| `home-dashboard.png` | Home with the Focus widgets and sidebar visible | 1440 × 900 |
| `tasks-board.png` | Tasks in Board view, with representative one-off and recurring cards | 1440 × 900 |
| `tasks-list.png` | Tasks in List view with an overflow menu open | 1440 × 900 |
| `calendar.png` | Calendar on a month containing several due dates | 1440 × 900 |
| `games.png` | Games board with cover art and an inline rating visible | 1440 × 900 |
| `command-palette.png` | `Ctrl+K` open with grouped search results | 1440 × 900 |
| `mobile-tasks.png` | Tasks on mobile with the navigation drawer closed | 390 × 844 |

## Capture checklist

1. Use the dark theme and an authenticated production-like account.
2. Collapse personal/sensitive notes, tokens, paths, and Admin secrets.
3. Use representative data; avoid empty pages and identifying information.
4. Keep browser zoom at 100% and hide browser developer tools.
5. Capture PNG files at the exact names above—README markup is already staged
   in an HTML comment and can be uncommented once the files exist.
6. Prefer lossless optimization (`oxipng` or equivalent) without resizing.

The application intentionally uses stable page headers, persistent sidebar
width, drawers, and deterministic Board/List layouts so captures remain aligned
and comparable across releases.
