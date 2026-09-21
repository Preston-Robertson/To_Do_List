# Stable Releases And Testing

## Branches

`main` is the production source. `testing` carries day-to-day development and
integration changes. Keep the same Git history; do not recreate main as an
unrelated branch or force-push release history. The initial launch-preparation
checkpoint can exist on both branches. Subsequent testing commits do not become
production updates until explicitly reviewed and promoted to main.

Open a pull request from testing to main for a release. Require independent
`offline-regression` CI and human review. Review the exact tested commit, backup
requirements, and deployment checks before merging. Branch protection should
require pull requests, current passing checks and administrator enforcement;
the local application never grants itself a bypass.

Use an explicit release tag such as `v1.0` only after release approval. Existing
maintainer release approval retains literal labels such as `1.01` -> `v1.01`.
The independent host/module SDK packages currently remain on the compatible
0.2 line; a product release tag does not silently change those constraints.

## Production Updates

Admin self-update always fetches `refs/heads/main` from `origin`, verifies the
commit, selects local main, and fast-forwards only. It never uses a configurable
testing branch, merges testing changes, resets local commits, or falls back when
main is unavailable. An old clean branch can move to main only when both its
HEAD and any existing local main are ancestors of the selected stable commit.
Dirty, divergent, or detached checkouts stop without dependency installation.

Before changing code, the updater retains the existing HEAD in a local
`rollback/pre-update-<timestamp>-<commit>` branch and displays that checkpoint.
It installs pinned dependencies only after verifying the selected stable HEAD.
Restart is separate. Dependency failure is not a successful update and does
not trigger a restart. Independently installed feature wheels remain pinned to
explicit release tags/checksums, not testing branch heads.

For the first migration, deployments already following main can obtain this
checkpoint through their existing updater. Older deployments on another branch
must receive the transition checkpoint or be moved to main by their operator;
the old updater cannot acquire new branch-selection behavior before it has
received the code. No production service is changed just by publishing Git refs.

## Rollback

The checkpoint preserves code, not databases or the Python environment. Before
an update, retain tested database backups and the previous deployment/runtime.
An operator can stop the service and check out the displayed rollback checkpoint
in a clean checkout, rebuild the matching pinned environment, then restart.
Do not blindly reverse schema changes or overwrite records with an old backup.
The updater deliberately refuses detached rollback checkouts until an operator
explicitly returns them to the stable branch.

## 1.0 Acceptance

- Run the full offline suite, wheel contracts, template/route validator and
  synthetic desktop/mobile checks on the final commit.
- Verify production follows main and development follows testing.
- Verify the deployed preview helper uses a separate PostgreSQL backup copy,
  dedicated role and OS identity. Automated agent previews stay synthetic-only.
- Validate Linux sandbox/image/cgroup isolation, dedicated HTTPS preview origin,
  SMTP delivery, GitHub token scopes and branch protection before enabling
  maintenance review or release gates. They default off.
- Preserve ignored local agent instructions and secrets outside publication;
  the packaged feedback-agent policy is part of the shipped module.

Publishing this preparation checkpoint does not start services, enable workers,
send email, create a product release, or replace production data.
