# Security and privacy

## Supported deployment

Luigi Web is designed for a private deployment. Use HTTPS before storing real
financial data and set `LUIGI_WEB_SECURE_COOKIES=1`.

## Sensitive data policy

Do not place these values in issues, screenshots, source files, fixtures, or
logs:

- passwords, tokens, private keys, and service-account documents;
- account, routing, card, tax, or government identifiers;
- names, email addresses, postal addresses, or phone numbers;
- real transaction descriptions or precise personal balances;
- private hostnames, LAN addresses, or deployment identifiers.

Finance accepts aliases and financial values only. It has no fields for legal
names or financial account identifiers. CSV import data is parsed in memory and
is not retained as an uploaded file.

## LLM isolation

Assistant prompts, chat history, and results from the allow-listed task tools
are sent to the configured provider. With `LUIGI_WEB_LLM_PROVIDER=copilot`,
that provider is GitHub Copilot and usage is governed by the authenticated
account or organization policy.

Finance routes and repository functions are not registered as LLM tools and are
not included in global search or chat context. Do not add finance access to the
assistant without an explicit, separately reviewed opt-in design.

The Copilot SDK runs in empty mode with only Luigi Web's custom task tools.
Shell, filesystem, web, MCP, skills, host instructions, and unrelated process
environment secrets are not exposed to the Copilot runtime.

## Authentication

- Main application access uses `LUIGI_WEB_UI_TOKEN`.
- Finance access additionally uses `LUIGI_WEB_FINANCE_TOKEN`.
- Browser mutations require a CSRF token.
- Cookies are HttpOnly and SameSite=Strict; Secure is enabled through
  `LUIGI_WEB_SECURE_COOKIES=1`.

Use long, random, distinct values for the UI and Finance tokens.

Authentication tokens are not editable through the web Admin page. A user who
has the main UI session must not be able to replace the Finance credential and
then unlock Finance. In production, load both tokens from a root-owned systemd
environment file outside the service's writable paths. The supplied service
example uses `/etc/luigi-web/credentials.env` with mode `0600`; it is loaded
after the Admin-managed environment file so protected values take precedence.
Creating or rotating either token requires host-administrator access.

## Storage and backups

- Shared task data lives in the LuigiBot PostgreSQL database.
- Finance lives in the app-owned SQLite path from `LUIGI_WEB_FINANCE_DB`.
- Finance database files, exports, and backups are gitignored.
- Downloaded exports use `Cache-Control: no-store`.
- Store backups on encrypted media with access controls appropriate for
  financial information.

## Reporting a vulnerability

Do not open a public issue containing secrets or private data. Report the
problem privately to the repository owner with only the minimum reproduction
information needed. Replace all identifiers and values with synthetic data.
