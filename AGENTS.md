# Luigi Web Agent Guide

## Purpose

Luigi Web is a server-rendered FastAPI/Jinja2 application. It is a secondary
read/write client for LuigiBot's PostgreSQL task schema and also owns optional
web-only features, including Finance.

## Privacy and PII

- Never commit, log, document, screenshot, or use in tests: real names, email
  addresses, account numbers, routing numbers, card numbers, tax identifiers,
  addresses, phone numbers, credentials, tokens, precise personal balances, or
  transaction descriptions copied from real statements.
- Examples and fixtures must use synthetic labels such as `Everyday account`,
  `Brokerage`, `Example merchant`, and obviously fictional amounts.
- Finance records must never be sent to the LLM, included in chat tools, global
  search, telemetry, error details, URLs, or public screenshots.
- Store money as integer minor units plus ISO currency. Never use floating
  point for persisted financial values.
- Finance CSV imports are previewed and normalized locally; raw uploads are not
  retained after the request.
- Secrets belong in process environment files and must remain gitignored.
- Raw Feedback stays local. Only an explicitly approved, privacy-screened copy
  may enter the maintainer queue, Copilot prompt, email notification, or draft
  pull request. Never copy Finance, task, card, character, or environment data.

## Data ownership

- LuigiBot owns `schema_version` and the shared tables: `tasks`,
  `recurring_tasks`, `discipline_list`, `discipline_completions`, and
  `follow_up_tasks`.
- Do not introduce destructive DDL or bump LuigiBot's schema version here.
- Web-owned task metadata must degrade safely if the web role cannot ALTER the
  shared tables.
- Finance uses a separate app-owned SQLite database configured by
  `LUIGI_WEB_FINANCE_DB`. It must not share tables with LuigiBot.
- Trading Cards uses one separate app-owned SQLite database configured by
  `LUIGI_WEB_CARDS_DB`. Its catalog, decks, collection, and price history stay
  together for transactional joins, but never share tables with LuigiBot or
  Finance.
- Characters uses a separate app-owned SQLite database configured by
  `LUIGI_WEB_RPG_DB`. Character, level-state, and sheet-entry records must not
  share tables with LuigiBot, Finance, or Trading Cards.
- Autonomous maintenance uses a separate SQLite queue configured by
  `LUIGI_WEB_MAINTAINER_DB`; it must not share a database with raw Feedback or
  any application data domain.
- The coordinated shared Discipline redesign is documented in
  `docs/discipline-v2-plan.md`.

## Security

- All mutating browser requests require authentication and a same-origin CSRF
  token. Bearer-token API clients remain supported without cookie CSRF.
- Finance requires a separate `LUIGI_WEB_FINANCE_TOKEN` unlock in addition to
  the main UI session.
- Cookies are HttpOnly and SameSite=Strict where compatible. Set
  `LUIGI_WEB_SECURE_COOKIES=1` when served over HTTPS.
- Never expose FastAPI docs, stack traces, environment values, filesystem paths,
  or finance records to unauthenticated clients.
- Finance exports and backups use `Cache-Control: no-store` and should be kept
  outside the repository.
- Trading-card deck and collection records stay out of LLM tools and external
  search. Scryfall bulk files are temporary, image URLs are allow-listed, and
  all persisted prices use integer minor units plus currency.
- Character records stay out of LLM tools and global record search. Store only
  user-entered rules summaries or explicitly imported open-license records with
  source/license metadata. Do not mirror compendium datasets in the repository.
- The maintainer may create draft PRs only from explicitly approved queue
  records. It must not merge, deploy, execute generated code while holding
  secrets, consume email replies, or grant itself broader tools or permissions.

## Architecture

- `app.py`: compatibility entry point for existing `uvicorn app:app` deployments.
- `luigi_web/application.py`: core FastAPI host, middleware, module composition,
  startup, and transitional shared helpers for already-existing modules.
- `luigi_web/core/module_registry.py`: versioned manifests, built-in catalog,
  approved installed entry points, dependencies, routes, and lifecycle hooks.
- `luigi_web/core/module_settings.py` / `modules_routes.py`: atomic validated
  next-restart selection and authenticated module management; environment-managed
  selections are read-only in the UI.
- `luigi_web/core/templating.py`: shared shell context, namespaced external
  templates, and packaged module assets.
- `luigi_web/core/cli.py`: `luigi-web` command with application-free `--help`
  and one Uvicorn worker.
- `luigi_web/modules/<id>/manifest.py` / `routes.py` / `templates/`: feature
  declarations, HTTP controllers, and domain pages/partials. Built-in IDs are
  tasks, discipline, planning, media, cards, characters, finance, assistant,
  admin, preview, and feedback; all remain selected by default.
- Planning requires Tasks and Discipline; Assistant requires Tasks, Discipline,
  and Media. Disabled modules do not mount routes or run lifecycle hooks.
- `luigi_web/auth.py`: main session, finance unlock, and CSRF helpers.
- `luigi_web/modules/tasks/repository.py`: LuigiBot shared-schema adapter used
  by both Tasks and Discipline; `luigi_web/db.py` is its identity alias, not an
  independent Discipline repository. The host still imports this bundled adapter.
- `luigi_web/modules/tasks/operations.py` / `backup.py` / `events.py` /
  `recurrence.py`: task rules, shared-task backup, LuigiBot event-ledger adapter,
  and dependency-free recurrence/calendar projection math.
- `luigi_web/modules/planning/repository.py`: app-owned Daily/Weekly Review state.
- `luigi_web/modules/finance/repository.py` / `routes.py`: app-owned Finance
  imports, reports, audit log, and separately authenticated HTTP routes.
- `luigi_web/modules/cards/repository.py` / `routes.py`: trading-card catalog,
  deck, collection, and authenticated HTTP routes; `importer.py`, `scryfall.py`,
  and `pokemon.py` own text import and authenticated catalog refresh providers.
- `luigi_web/modules/characters/repository.py` / `routes.py`: isolated character
  sheets, level states, calculations, and authenticated HTTP routes; `srd.py`
  owns explicit bounded 2014 SRD refresh and normalization, with no startup
  networking or retained raw bulk files.
- `luigi_web/modules/feedback/repository.py` / `routes.py`: local Feedback inbox
  and explicit maintainer approval UI; `maintainer.py` owns the privacy-screened
  durable queue, and `maintainer_agent.py` / `maintainer_worker.py` own bounded
  coding tools and the one-job draft-PR controller.
- `luigi_web/modules/preview/service.py` / `routes.py`: constrained Preview
  helper client/UI.
- `luigi_web/modules/media/service.py`: Game'N'Watch Google Sheets and public
  catalog integrations.
- `luigi_web/modules/assistant/providers.py` / `tools.py`: OpenAI-compatible
  and isolated GitHub Copilot providers with bounded tools.
- `luigi_web/modules/admin/environment.py`: allow-listed Admin environment editor.
- Legacy implementation imports use `sys.modules` identity shims; core APIs,
  rather than transitional application globals, define the external-module API.
- `luigi_web/clock.py`: configured single-user timezone and legacy UTC conversion.
- `luigi_web/paths.py`: packaged resource roots and optional `LUIGI_WEB_DATA_DIR`,
  source-checkout, or installed per-user writable defaults.
- `luigi_web/core/templates/`: four shared shell/login/module/command templates.
- `luigi_web/core/static/`: local CSS, JavaScript, IBM Plex Sans fonts, Lucide
  icons, and vendored browser libraries; public `/static` URLs are unchanged.
- External package resources use namespaced templates and `/module-assets/{id}`.
- `pyproject.toml`: installable `luigi-web` 0.1.0 host, Python 3.11+, with runtime
  dependencies retained from `requirements.txt` and packaged templates/assets.
- `examples/example-module/`: independently installable version-1 module using
  core APIs; `docs/modules.md` documents selection, packaging, and module contracts.
- `tests/`: offline regression tests using synthetic data only.

## Editing rules

- Preserve LuigiBot-compatible column spellings, including `catagory`.
- Keep changes focused and avoid unrelated rewrites.
- Prefer parameterized SQL and transactional repository methods.
- Verify writes before reporting success; never let UI state imply an
  uncommitted or failed mutation.
- Do not add third-party CDN dependencies. Browser assets are served locally.
- Rules providers must use fixed allow-listed bulk URLs, strict response limits,
  explicit user-triggered refresh, and complete source/license attribution.
- New public documentation must use placeholders such as `<postgres-host>` and
  must not include private hostnames, container IDs, LAN addresses, or usernames.
- Preview mutations require a separate deployment unlock and the fixed
  root-owned helper. Never add arbitrary shell, branch, path, or systemd input.
- Feedback remains local-only unless the user explicitly reviews and exports it.

## Validation

From the repository root:

```powershell
pip install "setuptools>=68" wheel
python -m unittest discover -s tests -v
python scripts/validate_repo.py
git diff --check
```

Packaging tests build and install host/example wheels in disposable directories.
The wheel test class is skipped if `setuptools` or `wheel` is missing; skipped
checks do not establish wheel verification. The validator compiles the shared
template loader's HTML and checks selected mounted routes without startup hooks;
full built-in route coverage requires selecting all built-ins. The example's
packaging tests also validate its external template namespace and assets.

Also validate:

- all Jinja templates compile;
- route declarations are unique;
- `git diff --check` is clean;
- responsive UI behavior at 1440x900 and 390x844 when frontend code changes;
- finance tests contain only synthetic data and leave no database files behind.

## Deployment

- Public deployment guidance belongs in README and uses generic placeholders.
- Machine-specific notes belong in gitignored `LOCAL_DEPLOYMENT.md`.
- The systemd example must not hardcode a personal IP, container ID, or secret.
- The service intentionally runs one Uvicorn worker while undo, chat history,
  and live integration clients remain process-local.
