# Isolated Preview deployment

Preview tests one allow-listed remote Git branch without switching the
Production checkout. It uses fixed host paths, a separate systemd service,
separate credentials, separate writable data, and a separate PostgreSQL task
snapshot. Finance data is never copied.

## Security boundary

- Main UI authentication can view Preview metadata but cannot mutate Preview.
- Mutations require the separate root-configured `LUIGI_WEB_DEPLOY_TOKEN`.
- The web process cannot execute arbitrary shell commands. It can invoke only
  the root-owned `/usr/local/sbin/luigi-web-preview` helper through a constrained
  sudoers rule.
- The helper accepts only `status`, `branches`, `create <remote-branch>`,
  `update`, `restart`, and `remove`.
- Worktree, service, environment, runtime, and data paths are fixed in the
  helper. Remote branch names are read from `origin/*` and validated again by
  the helper.

## Fixed host paths

| Purpose | Path |
|---|---|
| Production checkout | `/opt/luigi-web` |
| Preview worktree | `/opt/luigi-web-preview` |
| Preview Python runtime | `/opt/luigi-web-preview-runtime` |
| Preview writable data | `/opt/luigi-web-preview-data` |
| Preview environment | `/etc/luigi-web/preview.env` |
| Preview service | `luigi-web-preview.service` |

## Prerequisites

Provision a dedicated local PostgreSQL database and role for Preview. The role
must own the Preview database/schema so the helper can replace its contents,
but it must not be able to connect to or mutate the Production database.

Create `/etc/luigi-web/preview.env` as `root:root`, mode `0600`, using synthetic
placeholders until real local values are supplied:

```text
LUIGI_WEB_PG_HOST=<preview-postgres-host>
LUIGI_WEB_PG_PORT=5432
LUIGI_WEB_PG_DB=<preview-database>
LUIGI_WEB_PG_USER=<preview-role>
LUIGI_WEB_PG_PASSWORD=<preview-role-password>

LUIGI_WEB_UI_TOKEN=<preview-ui-token>
LUIGI_WEB_FINANCE_TOKEN=<preview-finance-token>
LUIGI_WEB_DEPLOY_TOKEN=<preview-deploy-token>
LUIGI_WEB_BIND=0.0.0.0
LUIGI_WEB_PORT=8081
LUIGI_WEB_SECURE_COOKIES=1

LUIGI_WEB_FINANCE_DB=/opt/luigi-web-preview-data/finance.db
LUIGI_WEB_FEEDBACK_DB=/opt/luigi-web-preview-data/feedback.db
LUIGI_WEB_TASK_METADATA_FILE=/opt/luigi-web-preview-data/task-web-metadata.json
LUIGI_WEB_COPILOT_HOME=/opt/luigi-web-preview-data/copilot
LUIGI_WEB_LLM_PROVIDER=disabled
LUIGI_WEB_GNW_SHEET_ID=
LUIGI_WEB_GNW_CREDS_FILE=
```

Add a distinct `LUIGI_WEB_DEPLOY_TOKEN` to Production's root-owned
`/etc/luigi-web/credentials.env`. Do not put it in the Admin-managed file.

The host needs `git`, `python3-venv`, `pg_dump`, `pg_restore`, `psql`, systemd,
and sudo. Run the one-time installer from the Production checkout:

```sh
sudo /opt/luigi-web/scripts/install_preview_helper.sh
```

The installer copies the helper and service unit to fixed root-owned paths,
validates the narrow sudoers rule with `visudo`, and reloads systemd. It does
not create PostgreSQL roles/databases or copy task data by itself.

## Lifecycle

- **Create:** fetches `origin`, creates a detached worktree at the selected
  remote ref, installs dependencies into the isolated runtime, replaces the
  Preview database from a local `pg_dump`, and starts the Preview service.
- **Update snapshot:** refuses a dirty worktree, fast-forwards to the selected
  remote ref in detached mode, refreshes dependencies and the local task
  snapshot, then restarts Preview.
- **Restart:** restarts only `luigi-web-preview.service`.
- **Remove:** stops/disables Preview, clears the Preview PostgreSQL schema,
  removes worktree/runtime/writable data, and leaves Production untouched.

Database dump files and passwords are temporary/local only. The helper does
not print task rows, credentials, dump contents, or environment values.