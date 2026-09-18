# Autonomous maintainer

Phase 1 converts explicitly approved Feedback items into reviewed draft pull
requests. It does not merge, deploy, read Production records, or process email
replies as authorization.

## Flow

1. An authenticated user records an item in the local Feedback inbox.
2. The user writes acceptance criteria, reviews the text for private data, and
   selects **Queue for maintainer**.
3. Luigi Web copies only category, message, local page path, acceptance
   criteria, feedback UUID, timestamp, and policy version into the separate
   maintainer queue. Email addresses, credential-shaped values, bearer tokens,
   and URL query strings are redacted during the copy.
4. The daily systemd timer claims one queued job and creates a fresh worktree
   from the configured remote `main` branch in a private worker directory.
5. GitHub Copilot receives the sanitized job and bounded repository tools. It
   has no shell, web, MCP, deployment, Git, email, queue, or secret tool.
6. The controller independently checks changed paths, size, whitespace,
   syntax, and likely PII or secrets. Passing work is committed, pushed, and
   opened as a draft pull request.
7. Pull-request CI runs the full offline suite, compiles all Jinja templates,
   validates unique routes, and checks whitespace without repository secrets.
8. The Feedback inbox shows the result. Optional email announces draft PRs,
   failures, and questions that need human attention.

Raw Feedback, Finance, task records, cards, characters, environment values,
and deployment credentials are never copied into the maintainer queue.

## Host layout

| Purpose | Path | Access |
|---|---|---|
| Worker clone, worktrees, Copilot runtime | `/var/lib/luigi-maintainer` | `luigi-maintainer` only |
| Sanitized queue | `/var/lib/luigi-maintainer-queue/maintainer.db` | web and worker queue group |
| Worker credentials | `/etc/luigi-web/maintainer.env` | root/systemd only |
| Installed environment example | `/etc/luigi-web/maintainer.env.example` | root-readable template |

The web service can write the queue but cannot read the worker clone or worker
credentials. The worker does not receive the web, Finance, PostgreSQL, cards,
RPG, Preview, or deployment environment files. Its systemd mount namespace
also hides the Production data directory, credentials files, metadata fallback,
and local deployment/feature notes from the worker process.

## Prerequisites

- Linux with systemd, `git`, and GitHub CLI (`gh`).
- The existing `/opt/luigi-web` checkout and virtual environment.
- A protected `main` branch that requires the **Maintainer PR validation**
  workflow and at least one human approval.
- A separate fine-grained GitHub token or GitHub App installation token with
  repository **Contents: read/write** and **Pull requests: read/write** only.
  Do not grant administration, workflow, environment, or branch-protection
  bypass permissions.
- A distinct token accepted by the GitHub Copilot SDK. Do not reuse the web UI,
  Finance, Preview, SMTP, or Git publishing credential.
- Optional SMTP credentials for outbound notification.

The GitHub credential can technically update branches and pull requests. The
worker implementation only pushes `automation/feedback-*` branches and calls
`gh pr create --draft`; branch protection is the independent control that
prevents autonomous merging.

## Installation

Run the idempotent installer from the Production checkout:

```sh
sudo /opt/luigi-web/scripts/install_maintainer.sh
```

The installer creates dedicated users/groups/directories, installs the service
and timer, and adds a systemd drop-in that gives Luigi Web access only to the
sanitized queue. It does not manufacture or print credentials. It leaves the
timer disabled until `/etc/luigi-web/maintainer.env` exists.

Create that root-owned file from the installed example, replace every required
placeholder, and keep mode `0600`. Re-running the installer then enables the
daily timer. The committed `maintainer.env.example` is documentation only and
must never contain real values.

The timer runs one job daily at 03:30 local host time with up to 30 minutes of
random delay. `Persistent=true` runs a missed invocation after the host returns.
Changing the schedule requires editing `luigi-maintainer.timer`, reviewing the
change, and reinstalling it.

## Protected changes

Phase 1 automatically permits only bounded text changes outside sensitive
surfaces. It asks for human attention rather than changing:

- authentication, CSRF, global browser security, or base layout;
- database adapters, backups, storage paths, Finance, cards, or characters;
- LLM tools/providers, external integrations, or Preview deployment;
- dependencies, service units, scripts, workflows, or maintainer policy;
- shared task event contracts or the Feedback approval boundary;
- more than 12 files or 1,200 changed lines;
- binary files, credentials, private keys, non-example emails, long numeric
  identifiers, private IP addresses, or machine-specific user paths.

These limits are intentionally conservative. A `Needs attention` result is a
request for normal human development and review, not an invitation to loosen
the worker at runtime.

## Notifications and approval

SMTP is optional and uses authenticated TLS: STARTTLS by default or implicit
TLS when `LUIGI_MAINTAINER_SMTP_SSL=1`. Notifications contain only queue UUIDs,
sanitized bounded summaries/questions, the draft PR URL, and an optional link
to the authenticated Feedback page.

Email replies are never consumed. They cannot approve permissions, merge code,
change worker policy, or deploy a release. Review decisions happen through the
protected GitHub branch and the authenticated Luigi Web interface.

## Failure behavior

- A worker run claims at most one item. A six-hour stale `Running` item becomes
  `Failed` on the next invocation.
- Configuration errors fail before a queue item is claimed and appear in the
  service journal without secret values.
- Agent, Git, GitHub, validation, and timeout errors become bounded `Failed`
  records and optional email notifications.
- Protected or oversized changes become `Needs attention` and are not pushed.
- A successful push is never merged or deployed by this worker.
- Phase 1 jobs are immutable and single-use. Submit a revised Feedback item
  after resolving a failure or clarification instead of mutating job history.

## Validation

Local development uses:

```powershell
python -m unittest tests.test_maintainer tests.test_feedback -v
python scripts/validate_repo.py
python -m unittest discover -s tests -v
git diff --check
```

Frontend changes also require the normal desktop and mobile browser checks.
On the Linux deployment host, the installer and systemd unit should be verified
before enabling the timer. Do not test the worker with Production feedback or
data; use an obviously synthetic approved item and inspect its draft PR.