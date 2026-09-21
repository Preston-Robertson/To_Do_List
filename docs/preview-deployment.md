# Isolated Preview deployment

Preview tests one allow-listed remote Git branch without switching the
Production checkout. It uses fixed host paths, a separate systemd service,
separate credentials, separate writable data, and a separate PostgreSQL task
snapshot. Finance data is never copied.

This is the manual, human-reviewed branch Preview. The feedback-agent test
application stays in its network-disabled sandbox with synthetic records only;
it must never receive this backup or its credentials. Open manual Preview from
Admin to exercise trusted branch changes against a private copy of task data.

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
- Preview runs as its own `luigi-web-preview` OS account, with no deployment
  sudo permission. Production files and credential directories are hidden.
- Every service start runs the root-owned database isolation preflight, even
  when started directly through systemd. The preview database name, role, and
  password must differ from Production. A different hostname is not sufficient.
- The target connection must report the exact configured database and role,
  no elevated privileges or role memberships, and no CONNECT privilege to the Production
  database if it exists on that PostgreSQL cluster. Failed checks block start,
  snapshot restore, and clearing; they never fix privileges automatically.

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
Use a dedicated PostgreSQL instance where possible. On a shared instance,
account for PostgreSQL's default PUBLIC CONNECT privilege when provisioning
access; merely granting a role access to Preview does not deny Production.
Configure PostgreSQL authentication/network rules to deny the preview role on
the production database, including through alternate hostnames. The helper
checks target-cluster privileges, not remote clusters or firewall rules.
Do not grant SUPERUSER, CREATEDB, CREATEROLE, REPLICATION, BYPASSRLS, or membership
in any other role, including predefined server-file/program roles. The preview
service must not inherit production tokens.

Create `/etc/luigi-web/preview.env` as `root:root`, mode `0600`, using synthetic
placeholders until real local values are supplied:

```text
LUIGI_WEB_PG_HOST=<preview-postgres-host>
LUIGI_WEB_PG_PORT=5432
LUIGI_WEB_PG_DB=<preview-database>
LUIGI_WEB_PG_USER=<preview-role>
LUIGI_WEB_PG_PASSWORD=<preview-role-password>

LUIGI_WEB_UI_TOKEN=<preview-ui-token>
LUIGI_WEB_BIND=0.0.0.0
LUIGI_WEB_PORT=8081
LUIGI_WEB_SECURE_COOKIES=1

LUIGI_WEB_DATA_DIR=/opt/luigi-web-preview-data
LUIGI_WEB_MODULES=tasks,discipline,planning
LUIGI_WEB_TASK_METADATA_FILE=/opt/luigi-web-preview-data/task-web-metadata.json
LUIGI_WEB_LLM_PROVIDER=disabled
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
create a separate preview OS identity, but does not create PostgreSQL
roles/databases or copy task data by itself. Reinstall the updated helper and
unit together for an existing deployment. Existing preview files must be
accessible only to the dedicated preview identity; old shared-account ownership
may need an operator migration. Installation does not restart the service.

## Lifecycle

- **Create:** fetches `origin`, creates a detached worktree at the selected
  remote ref, installs the trusted checkout's dependencies into the isolated
  runtime, restores a local `pg_dump` into Preview, and starts the service.
- **Update snapshot:** refuses a dirty worktree, fast-forwards to the selected
  remote ref in detached mode, refreshes dependencies and the local task
  snapshot, then restarts Preview.
- **Restart:** restarts only `luigi-web-preview.service`.
- **Remove:** stops/disables Preview, clears the Preview PostgreSQL schema,
  removes worktree/runtime/writable data, and leaves Production untouched.

Database dump files and passwords are temporary/local only. The helper does
not print task rows, credentials, dump contents, or environment values.
The dump reads Production with read-only transaction settings. It lives in a
private temporary directory outside the preview app's writable data and is
deleted after success or failure. Restore uses `--single-transaction` and
`--exit-on-error`; it never runs DROP/restore against the source connection.
Source and target credentials are supplied only to the trusted helper, while
the preview application receives only the target credentials. Test edits stay
in that copy; refreshing the snapshot discards test changes to copied objects.

This is for trusted, human-reviewed branches, not hostile generated code.
Do not point automatic candidate execution at this service. Live PostgreSQL
permissions and Linux systemd isolation require deployment acceptance; mocked
offline tests cannot verify a host's installed helper, roles, or firewall.