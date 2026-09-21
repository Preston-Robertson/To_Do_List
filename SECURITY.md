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

Preview deployment uses a third protected `LUIGI_WEB_DEPLOY_TOKEN`. It is not
editable through Admin and grants a one-hour HttpOnly deployment session only
under `/admin/preview`. The web service still cannot run arbitrary privileged
commands: Preview mutations pass through the fixed root-owned helper and narrow
sudoers rule documented in `docs/preview-deployment.md`.

The local Feedback database is excluded from the Assistant and global search.
Exports require explicit review and are served with no-store cache controls.
An explicit **Queue for maintainer** action separately approves a redacted copy
of selected request fields and acceptance criteria for GitHub Copilot and a
maintenance review. The local worker still uses remote Copilot inference; it
is not offline or local-only inference. Raw Feedback and its database are not
exposed to the worker. Approval never includes Finance, task, card, character, environment, or
deployment records.

The maintenance worker runs separately from the web identity. Its Copilot session
uses custom bounded repository tools only; shell, web, MCP, Git, deployment,
email, queue, and secret tools are disabled. Generated code executes only in a
secret-free rootless Linux sandbox or independent read-only CI, never in a host
process holding tokens. The image is immutable, pinned locally, and built from
reviewed source before secrets are provisioned; runtime networking and secret
mounts are forbidden. Rootless containers still share the host kernel.

Generation/fetch, publication, refresh, SMTP, release, and preview use distinct
role environments. Only release receives `LUIGI_RELEASE_GITHUB_TOKEN`; it has
no branch-protection bypass. Current controllers share a trusted OS identity and
private state containing Copilot runtime files: separate environment files are
not complete process isolation. The notifier and gateway have separate identities.
All role secrets are root-owned, outside application-readable/writable paths.

Protected `LUIGI_WEB_MAINTAINER_REVIEW_ENABLED` and `LUIGI_WEB_RELEASE_ENABLED`
default off. Human option selection authorizes publication only. Explicit
authenticated, CSRF-protected release approval binds a version to the selected
option, exact published HEAD, successful independent CI, and live preview
evidence. The separate controller rechecks required `offline-regression` status,
strict branch protection, human review, and tag availability before merge/tag;
the coding agent cannot approve or release. No controller deploys production.
Partial remote outcomes require reconciliation, not blind retries.

Candidate content is served only by a separately authenticated HTTPS gateway on
a different hostname, never at the production origin. The production landing
sends a 60-second, single-use HEAD-bound ticket in a POST body. The gateway uses
a host-only Secure/HttpOnly/Strict session, proxies only a validated local Unix
socket, and receives no main UI, Finance, Copilot, GitHub, or release token.
Only the web ticket issuer and gateway share the protected gateway key; the
preview controller and candidate never receive it. Stop/expiry blocks access;
stopping clears readiness and blocks release. The shared queue contains audit
and bounded-retry notification outbox records, not raw Feedback. Emails contain
generic IDs and an authenticated review link, never approval capabilities or
private request text; replies are not consumed.

See [docs/autonomous-maintainer.md](docs/autonomous-maintainer.md) and
[docs/maintainer-test-preview.md](docs/maintainer-test-preview.md). Live Linux,
GitHub, SMTP, and HTTPS gateway integration remains a deployment acceptance
requirement, not a claim established by the offline suite.

## Storage and backups

- Shared task data lives in the LuigiBot PostgreSQL database.
- Finance lives in the app-owned SQLite path from `LUIGI_WEB_FINANCE_DB`.
- Finance database files, exports, and backups are gitignored.
- Raw Feedback and the sanitized maintainer queue use separate SQLite files.
- Maintainer credentials and its private clone live outside the repository and
  outside the web service's readable paths.
- Downloaded exports use `Cache-Control: no-store`.
- Store backups on encrypted media with access controls appropriate for
  financial information.

## Reporting a vulnerability

Do not open a public issue containing secrets or private data. Report the
problem privately to the repository owner with only the minimum reproduction
information needed. Replace all identifiers and values with synthetic data.
