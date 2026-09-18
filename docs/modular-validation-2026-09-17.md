# Modular Overhaul Validation

Checks used only synthetic records, temporary storage, and local assets.
No production records, private configuration, or external services were used.

## Structure and compatibility

- The host imports the canonical shared task implementation while legacy
  imports retain module identity and monkeypatch compatibility.
- Pylance reports no errors in the touched host and shared shell stylesheet.
- All 88 removed template/static paths have relocated resource destinations.
- The host and example extension have separate installable wheels. Packaging
  tests exercise approval, selection, authenticated routes, namespaced
  templates, local assets, and installed writable defaults outside the source
  checkout. Installation alone does not enable the example.
- Runtime selection remains next-restart configuration. Validation did not
  deploy the application or create separate feature repositories; source
  publication is independent of these checks.

## Browser checks

The disposable workspace preview was exercised at 1440x900 and 390x844.
Light-theme checks covered Home, Tasks, Calendar, Projects, Discipline, Cards
decks/detail, Characters, and the Finance unlock page. Dark-theme checks
covered Modules, Home, Tasks, Calendar, Discipline, Cards decks/detail, and
Characters. Modules was also inspected in light mode at both sizes.

- Checked pages returned HTTP 200 with no document-width overflow or browser
  JavaScript errors. Visible images, fonts, and local icons loaded.
- Desktop and mobile screenshots were inspected for Modules, Home, and Cards.
- Light/Dark selection persisted across page changes. System mode responded
  to light/dark browser preferences, and reduced-motion mode was exercised.
- Mobile navigation made the workspace inert while open, closed on Escape,
  and restored focus to its opener. Search supported typed navigation,
  keyboard selection, and Escape dismissal.
- Module filtering, empty results, clearing, transitive dependency toggles,
  and authenticated saves were exercised. Disabling Tasks deselected Planning
  and Assistant; saving showed restart-pending state while running navigation
  and the enabled count stayed unchanged. Re-enabling dependencies and saving
  restored the original selection.
- A synthetic deck quantity was saved, Stats was opened, and the quantity was
  restored and verified after reload.
- Home task rows were corrected to keep full titles above their metadata and
  retain a separate 40x40 completion target. Browser geometry checks confirmed
  no title clipping or button overlap on mobile or desktop.

The unused card-inspector image has no source until opened; its zero-sized
placeholder is not a missing visible asset.

## Repeatable checks

Final offline run: 309 tests, 307 passed, two Windows-specific maintainer
permission/symlink skips, and no failures or errors. The 85 focused module,
runtime, and packaging tests passed without skips. All wheel checks ran.
The validator compiled 82 templates and checked 198 unique registrations;
the synthetic preview checked nine views, and `git diff --check` was clean.

Use a clean development environment with no production credentials:

```powershell
python -m unittest discover -s tests
python scripts/validate_repo.py
python scripts/preview_workspace.py --check
git diff --check
```

Select all built-in modules for complete route coverage. Wheel tests require
setuptools and wheel; skipped wheel tests do not establish packaging coverage.
The Windows-specific maintainer permission/symlink tests may be skipped.

## Limits

The preview blocks live deployment and external refresh operations. It does
not replace testing a configured deployment, actual task writes against
LuigiBot, Finance records, or external integrations. Core-only startup and
module lifecycle behavior are covered by isolated runtime tests rather than
by changing the live preview's module set. External packages are trusted
in-process code, not sandboxed plugins.