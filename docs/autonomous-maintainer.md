# Maintenance Review: Phase 2 Operator Guide

Maintenance review turns an explicitly privacy-approved Feedback request into
two or three tested options. A human chooses an option before publication and
separately approves a versioned release after independent CI and an isolated
interactive preview match the published HEAD. It is not autonomous approval,
an application deployment service, or a declaration that version 1.0 has shipped.
The approval-gated merge/tag feature is implemented; these documentation changes
do not create a live release or version tag.

All examples are inactive deployment templates. Linux rootless Podman, systemd,
GitHub branch protection, SMTP delivery, and the HTTPS gateway must be validated
on the target host before activation. Offline tests do not establish those facts.

## Workflow

1. In the authenticated Feedback inbox, review the request and acceptance
  criteria for privacy, then explicitly queue the sanitized copy. Raw Feedback
  remains local. Never approve Finance, task, card, character, environment, or
  credential data for this workflow.
2. The generation role claims one approved request and proposes two or three
  alternatives using bounded repository tools. Each option gets secret-free
  offline tests, template/route checks, and synthetic screenshots at 1440x900
  and 390x844. Failure or insufficient evidence requires attention, not approval.
3. Only after all required options are ready does the notification outbox send
  an options-ready email. Open the authenticated Maintenance review page to
  inspect the changes, test evidence, and screenshots. Email contains generic
  state, opaque IDs, and an authenticated link, not request text or attachments.
4. A human selects an option and enters the literal version in the UI before
  approving publication. The publish role consumes that explicit approval,
  checks the saved evidence, and publishes only the selected option as a draft
  pull request. Publication is not release approval.
5. Independent GitHub CI and the refresh role verify the exact published HEAD.
  Complete the required GitHub review and ready-for-review steps. The separate
  preview controller starts a synthetic test application for that same HEAD.
6. Open **Open test application** from the authenticated review page. Candidate
  HTML runs at the separate preview origin, never the production origin.
  Generation screenshots alone are not an interactive-preview attestation.
7. After review, confirm the exact version chosen in step 4 and the published
  HEAD, then explicitly approve release in the UI. The version must match
  literally, not just numerically. The release controller rechecks the selected
  option, HEAD, CI, preview evidence, version/tag availability, and GitHub
  protection before merging and tagging. Changed HEAD or stopped preview
  invalidates readiness.

Version labels use `X.xx`, for example literal `1.01` and tag `v1.01`; preserve
the leading zero after the decimal. This user-facing release label is independent
of the core SDK/package compatibility version `0.2`. Do not change package
constraints to match it or infer a 1.0 launch from installing these templates.

## Trust Boundaries

The built-in agent receives the packaged
[Feedback agent operating instructions](../module-repos/feedback/src/luigi_web/modules/feedback/maintainer-policy.md)
from the trusted controller installation. They remain in published source and
module wheels. External development-agent notes and editor customizations stay
local and Git-ignored; cleanup removes their Git tracking, not their local files.

The worker process is local to the operator's host. GitHub Copilot inference is
remote: the normal Copilot provider still calls the external service. A
"localhost agent" is not an offline LLM. Only explicitly privacy-approved,
sanitized requests and permitted repository context may reach that provider.

The coding agent has bounded repository tools, not shell, Git, email, queue,
deployment, or secret tools. Trusted controllers own remote operations. Neither
the agent nor a scheduled role may choose an option or approve release for the
human. Main UI mutations require authentication and same-origin CSRF.

Generated code, tests, imports, dependency installation, and build hooks must
never execute in a host process holding credentials. Candidate execution is
confined to the pinned secret-free sandbox and independent CI. Rootless
containers share the host kernel and are not a hostile-code VM boundary.

Protected paths and bounded change limits still apply. Authentication, storage,
Finance, cards, characters, dependencies, workflows, service units, integrations,
and maintainer policy require normal human development when outside the agent's
allowlist. Do not loosen policy in response to a `needs_attention` result.

## Protected Web Configuration

Both deployment-owned gates default off:

```ini
LUIGI_WEB_MAINTAINER_REVIEW_ENABLED=0
LUIGI_WEB_RELEASE_ENABLED=0
```

An operator may set the review gate to exactly `1` after setup; release remains
off until its separate boundary is validated. Setting the release gate to `1`
only exposes the explicit approval workflow, never grants autonomous approval.
Keep these values outside the Admin-editable environment. The web host receives
the queue path, read-only screened artifact access, preview origins, and ticket key,
but no worker GitHub, Copilot, release, or SMTP credentials. See the
[web environment example](../examples/maintainer-review/web.env.example).

## Roles And Credentials

Use a trusted, reviewed installation at `/opt/luigi-web`, not a candidate
checkout, for all controller entry points. The compatibility command
`python -m luigi_web.maintainer_worker` delegates through `maintainer_worker.main`
to `review_worker.main`, whose default role is generation. It no longer dispatches
the Phase 1 worker. Explicit role commands use:

```text
python -m luigi_web.modules.feedback.review_worker --generate
python -m luigi_web.modules.feedback.review_worker --publish
python -m luigi_web.modules.feedback.review_worker --refresh
python -m luigi_web.modules.feedback.review_worker --notify
python -m luigi_web.modules.feedback.review_worker --release
python -m luigi_web.modules.feedback.review_worker --preview UUID
python -m luigi_web.modules.feedback.review_worker --preview-pending
python -m luigi_web.modules.feedback.review_worker --stop-preview
```

| Role | Environment file | Allowed credential |
| --- | --- | --- |
| Generation | `generate.env` | Copilot token and fetch-only GitHub token |
| Publication | `publish.env` | Separate Contents/Pull requests write token |
| Refresh | `refresh.env` | Read-only GitHub token for HEAD/check verification |
| Notification | `notify.env` | Outbound SMTP only |
| Release | `release.env` | `LUIGI_RELEASE_GITHUB_TOKEN` only |
| Preview controller | `preview.env` | Read-only repository fetch token only |
| Preview gateway | `gateway.env` | Preview ticket key only |

Generation, publication, refresh, and preview use the same configuration key
`LUIGI_MAINTAINER_GITHUB_TOKEN` in **different role files**, not the same token.
Prefer a fine-grained read-only token for generation; the inherited configuration
name does not require Contents write. Publication needs Contents and Pull
requests write. Release uses its own narrowly scoped merge/tag credential,
never branch-protection bypass, administration, workflows, or deployment rights.

Files live in `/etc/luigi-web/maintainer-review`, owned by root, mode `0600`,
loaded by root-owned system units. Do not source a combined credential file,
reuse the old all-role environment, put secrets in images, or load application
requirements/code while holding tokens. Install the trusted controller and
dependencies before provisioning any secrets.

The current `WorkerConfig` and private `0400` candidate files share
`LUIGI_MAINTAINER_STATE_DIR`, which also contains Copilot runtime state. The
generation, publication, refresh, release, and preview templates therefore use
one dedicated `luigi-maintainer` identity and serialize controller invocations
with `flock`. Non-generation roles hide the Copilot directory. Separate role
environments reduce exposure but **are not separate OS security boundaries**:
same-UID processes/private state remain mutually trusted. Independent identities
for every controller require a reviewed immutable-artifact handoff, not a
recursive permission relaxation or a promise that these examples provide it.
Notifier and gateway use distinct identities and cannot read private worker state.

## Storage

| Purpose | Default location | Boundary |
| --- | --- | --- |
| Clone, private patches/evidence, Copilot runtime | `/var/lib/luigi-maintainer` | Trusted controller identity only |
| Sanitized queue, audit, notification outbox, gateway sessions | `/var/lib/luigi-maintainer-queue/maintainer.db` | Web/controllers and dedicated queue group |
| Screened candidate patches and synthetic screenshots | `/var/lib/luigi-maintainer-queue/review-artifacts` | Controller writes; authenticated web reads after digest verification |
| Preview manifest/socket state | `/var/lib/luigi-maintainer-preview` | Controller writes; web/gateway read; gateway uses socket |
| Notifier/gateway homes | `/var/lib/luigi-maintainer-notify`, `/var/lib/luigi-maintainer-gateway` | Respective identity only |
| Rootless runtime | `/run/luigi-maintainer` | Controller identity only |

`LUIGI_MAINTAINER_ARTIFACT_DIR` selects the shared artifact root; the default is
`review-artifacts` beside the queue database. Only finalized, privacy-screened
patches and synthetic images are shared. Each option has its own patch artifact
UUID, and each screenshot has a separate screenshot artifact UUID:

| Artifact | Path relative to the shared root | Human review |
| --- | --- | --- |
| Screened patch | `<run-uuid>/<option-artifact-uuid>/candidate.patch` | Authenticated option diff; SHA-256 must match that option's `diff_sha256` |
| Desktop screenshot | `<run-uuid>/<desktop-screenshot-uuid>/desktop.png` | Authenticated option image; SHA-256 and 1440x900 dimensions must match its metadata |
| Mobile screenshot | `<run-uuid>/<mobile-screenshot-uuid>/mobile.png` | Authenticated option image; SHA-256 and 390x844 dimensions must match its metadata |

The screenshot UUIDs come from that option's screenshot metadata, not its patch
artifact UUID. Private `source.patch`, validation evidence, and Copilot state
remain under the controller-only state directory. Web-readable artifacts do
not grant unauthenticated access or authorize publication.

The gateway needs transactional write access to the queue for tickets/sessions,
not merely a read-only database connection. It must not have raw Feedback or
any application database. Its preview state access is read-only except connecting
to the `0660` socket. Keep the preview state parent outside a directory writable
by the web/gateway identities; SQLite queue access does not authorize replacing
preview manifests. Use quotas for exports, images, and artifacts. Back up the
queue as sensitive operational state outside public source or support bundles.

## Linux Sandbox Prerequisites

Require rootless Linux Podman at the fixed runtime path, seccomp, cgroup v2 with
delegated `cpu`, `memory`, and `pids` controllers, and a locally installed image
named `localhost/luigi-maintainer@sha256:<digest>`. Tags, runtime pulls, privileged
containers, host networking, and secret/application mounts are forbidden.
Runtime has no network; source/root filesystem are read-only, capabilities
are dropped, synthetic data uses tmpfs, and CPU/memory/process/time limits apply.

Build [the sandbox image](../examples/maintainer-sandbox.Dockerfile) separately
from a reviewed source revision and a small audited context containing only the
reviewed dependency file and fixed runners. An isolated build pool may use
controlled networking to resolve audited dependencies and browser binaries.
Build before provisioning secrets; never build from a proposed patch or install
candidate/application requirements on a credential-bearing worker. Transfer the
immutable image into the runtime identity's local image store. A digest pin
identifies reviewed bytes; it does not by itself prove the build was safe.

The supplied root-owned **system** services run under the dedicated unprivileged
account, with `Delegate=yes` for generation and preview. They are an explicit
alternative to a deployment-managed user service with delegated controllers,
not a claim that delegation works on every distribution/container host.
Provision nonoverlapping `/etc/subuid` and `/etc/subgid` allocations, the reviewed
`newuidmap`/`newgidmap` helpers, local Podman storage/cgroup configuration, and a
private `XDG_RUNTIME_DIR`. Review inherited Podman `mounts.conf` and hooks; no
sensitive automatic mounts or unreviewed hooks are allowed.

For these two runtime roles only, `NoNewPrivileges=false` permits the rootless
UID/GID mapping helpers, namespaces are not restricted, `ProtectControlGroups`
is off, and the capability bounding set is not emptied. Rootless Podman may need
user, mount, PID, network, UTS, IPC, and cgroup namespaces. The Phase 1 unit's
`NoNewPrivileges=true`, `RestrictNamespaces=true`, `ProtectControlGroups=true`,
and empty bounding set cannot simply be retained. These exceptions do not grant
root execution to the agent; they are host-runtime prerequisites to review.
Other roles retain those restrictions. Keep filesystem restrictions, private
temporary storage, and inaccessible production paths. Adapt host-specific data
paths before activation; examples cannot discover relocated sensitive mounts.

Only non-runtime roles load `restricted.conf`: device isolation, address-family
and syscall/architecture filters, personality locks, and kernel-protection
settings can block nested runtime setup or implicitly enable no-new-privileges
on supported older systemd releases. Do not apply that drop-in to generation or
preview. The common drop-in retains filesystem isolation and disables core dumps.
Validate effective settings, not just the literal `NoNewPrivileges=false` line;
review inherited/global drop-ins too. Kernel/device access remains subject to
the unprivileged host identity; candidate containers retain their own seccomp,
capability, device, and network restrictions.

Run the sandbox availability preflight and a synthetic full candidate check
under the **actual service identity and namespace**, not merely a root shell.
Verify rootless mode, controllers/limits, mapping helpers, local image lookup,
absence of inherited mounts/secrets, and runtime networking denial. Fail closed
on unsupported hosts. Do not weaken sandbox limits to make tests pass.

## Installation And Activation

The one-time installer is explicit and never starts/restarts the application,
enables timers, builds an image, installs Python dependencies, or reads tokens:

```sh
sudo /opt/luigi-web/scripts/install_maintainer.sh --install
```

No argument is an error. `--help` has no side effects. `--install` requires root,
installs templates/accounts/directories and a queue-only web drop-in, disables
existing maintainer timers, and refuses to replace active controllers. It keeps
any real role environment files unchanged. The old `maintainer.env` does not
trigger activation. The legacy daily unit remains disabled; the new generation
timer is separate. No live deployment action is performed by editing this repo.

Before an operator explicitly enables anything:

1. Review the trusted installation, dependency/image provenance, filesystem
  permissions, rootless runtime, and host-specific inaccessible paths. Confirm
  the services pass `systemd-analyze verify` on the target systemd version.
2. Provision only each role's allowed credentials from the examples. The default
  `LUIGI_MAINTAINER_REQUIRED_CHECKS` value is `offline-regression`, matching the
  emitted job check name. **Maintainer PR validation** is the workflow title,
  not a check name. An explicit override is only needed for intentionally
  different required job checks; keep it aligned with branch protection.
3. Protect `main`: require `offline-regression`, strict/up-to-date checks,
  at least one human review, no bypass for controller tokens, and a ready,
  mergeable PR. Never treat the release token as an administrator escape hatch.
4. Configure the separately authenticated HTTPS gateway and production landing
  as described in [the preview guide](maintainer-test-preview.md). Keep the
  release gate off while testing synthetic tickets, HEAD changes, stop/expiry,
  cookies, UDS permissions, and the proxy.
5. Verify installed `--help` dispatch and a synthetic end-to-end review before
  scheduling. Enable only reviewed units explicitly. The generation timer is
  daily at 03:30 with up to 30 minutes jitter; publish, refresh, notify, and
  release timers process at most one eligible item per minute. Timers consume
  existing human approvals; they do not create approvals. Gate activation and
  web restart are deliberate deployment operations, not installer side effects.

### Preview Scheduling

The public controller implements `--preview UUID`, `--preview-pending`, and
`--stop-preview`. The preview service calls `--preview-pending`; its scheduling
contract requires the oldest eligible `testing` run whose preview commit does
not match its published HEAD. Verify oldest-first selection with multiple queued
runs when validating the installed dispatch. With the sandbox available, no
eligible run means an idle result; an unavailable sandbox fails closed.

The controller does not replace an active slot, accept arbitrary paths/origins,
or grant approval. Reviewers use the authenticated UI, without per-run server
commands or systemd instance names. Keep the timer inactive until the operator
verifies the installed dispatch and Linux runtime/setup described above; source
implementation and offline checks do not establish deployment readiness.

The gateway command is independently available as
`python -m luigi_web.modules.feedback.test_preview --serve --port 58120`.
Stopping a preview must revoke readiness first via the protected controller;
simply stopping a timer or gateway is not a verified cleanup operation.

## Notifications, Failures, And Recovery

The dedicated SMTP role uses authenticated TLS (STARTTLS by default; implicit
TLS when `LUIGI_MAINTAINER_SMTP_SSL=1`). Durable outbox delivery has bounded
retries, at most five attempts, and audit records. Retries can produce duplicate
mail after ambiguous transport outcomes; email is not exactly-once approval.
No approval links, email reply processing, source patches, screenshots, private
records, or free-form feedback text belong in notifications.

Missing evidence, unavailable sandbox, changed HEAD, failed CI, expired/stopped
preview, version collision, or unverifiable remote outcomes block progress.
Inspect authenticated state and bounded audit/status messages; do not paste
private artifacts or environment files into support chat. A partial publish or
merge/tag outcome requires operator reconciliation against the exact PR, commit,
and tag before another action. Never resend release approval blindly: a merge
may have succeeded while tagging or recording its receipt failed.

The release controller merges/tags only the human-authorized identity. It does
not deploy production. Stopping the single active preview clears preview
readiness and blocks release; uncertain container cleanup leaves a blocking
manifest. Keep separate backups and ordinary release/deployment controls.

## Validation Status

The deployment regression test reads only source templates and synthetic values:

```sh
python -m unittest discover -s tests -p test_review_deployment.py -v
bash -n scripts/install_maintainer.sh
git diff --check
```

Static checks cannot validate rootless namespaces, cgroup delegation, image
contents, real GitHub policies/tokens, SMTP, HTTPS cookies, or preview UDS
transport. Those Linux/live-service checks remain unverified until an operator
performs the explicit synthetic deployment acceptance above. No credentials,
emails, remote mutations, installs, or live services are exercised by this guide.