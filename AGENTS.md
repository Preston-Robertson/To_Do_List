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

## Data ownership

- LuigiBot owns `schema_version` and the shared tables: `tasks`,
  `recurring_tasks`, `discipline_list`, `discipline_completions`, and
  `follow_up_tasks`.
- Do not introduce destructive DDL or bump LuigiBot's schema version here.
- Web-owned task metadata must degrade safely if the web role cannot ALTER the
  shared tables.
- Finance uses a separate app-owned SQLite database configured by
  `LUIGI_WEB_FINANCE_DB`. It must not share tables with LuigiBot.
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

## Architecture

- `app.py`: routes and view-model shaping.
- `auth.py`: main session, finance unlock, and CSRF helpers.
- `db.py`: LuigiBot shared-schema adapter.
- `finance.py`: app-owned finance repository, imports, reports, and audit log.
- `gnw.py`: Game'N'Watch Google Sheets and public catalog integrations.
- `llm.py`: OpenAI-compatible and isolated GitHub Copilot providers.
- `env_file.py`: allow-listed Admin environment editor.
- `templates/`: server-rendered pages and HTMX partials.
- `static/`: local CSS, JavaScript, icons, and vendored browser libraries.
- `tests/`: offline regression tests using synthetic data only.

## Editing rules

- Preserve LuigiBot-compatible column spellings, including `catagory`.
- Keep changes focused and avoid unrelated rewrites.
- Prefer parameterized SQL and transactional repository methods.
- Verify writes before reporting success; never let UI state imply an
  uncommitted or failed mutation.
- Do not add third-party CDN dependencies. Browser assets are served locally.
- New public documentation must use placeholders such as `<postgres-host>` and
  must not include private hostnames, container IDs, LAN addresses, or usernames.

## Validation

From the repository root:

```powershell
python -m unittest discover -s tests -v
```

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
