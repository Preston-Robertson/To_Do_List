# Isolated Maintainer Test Application

This is an opt-in controller and gateway, not a deployment or automatic release.
Use the protected default-off review/release gates and role deployment described
in [the Phase 2 operator guide](autonomous-maintainer.md). The preview CLI and
parent review routes are implemented. The preview timer remains an inactive
deployment template until the operator verifies Linux runtime/setup.
Never mount this gateway into the production FastAPI app,
or reverse-proxy candidate HTML, JavaScript, assets, or sockets at its origin.

## Protected Configuration

- `LUIGI_MAINTAINER_UI_URL`: canonical production HTTPS root origin.
- `LUIGI_MAINTAINER_PREVIEW_URL`: dedicated HTTPS root origin with a different
  hostname. Same-host ports, subpaths, credentials, queries, and fragments fail
  closed. Prefer a separate registrable domain; production cookies must remain
  host-only. These values belong in deployment-owned configuration, never forms.
- `LUIGI_MAINTAINER_PREVIEW_GATEWAY_KEY`: 32-64 cryptographically random bytes,
  encoded as lowercase hexadecimal. Share only between the trusted production
  landing/ticket issuer and separate gateway. Never put it in a URL, log, worker,
  sandbox environment, mount, or image. Rotation revokes tickets and sessions.
- `LUIGI_WEB_MAINTAINER_DB`: the existing approved queue database, shared with the
  gateway for transactional ticket/session writes, not a read-only DB mount.
  No raw Feedback/application DBs.
- `LUIGI_MAINTAINER_PREVIEW_STATE_DIR`: short protected absolute directory outside
  live source/data; defaults to `review-preview` beside the maintainer queue DB.
  The complete Unix socket pathname must fit within 103 encoded bytes.
- `LUIGI_MAINTAINER_SANDBOX_IMAGE`: the same pinned local image digest used for
  approved candidate checks. Mutable tags and runtime pulls are forbidden.

Provision the state parent outside application-writable trees. The dedicated
controller owns the state directory/manifest; gateway and main UI get read-only
group/ACL access, never directory write access. Keep private source exports
controller-only. The runtime directory is setgid, owner-writable, group-readable
and traversable; its socket is 0660. The rootless UID/GID mapping preserves the
controller's ownership. Give the gateway socket access through the queue group,
without granting either it or the web process permission to replace manifests.
Configure the queue group through the existing `LUIGI_MAINTAINER_QUEUE_DIR`.
Candidate containers receive only their own socket directory, not its parent.
Filesystem quotas and any required SELinux policy remain deployment concerns.

The service examples explicitly set `/var/lib/luigi-maintainer-preview`, outside
the group-writable queue directory. Use this override consistently in the web,
controller, and gateway. Do not use the queue-adjacent default where a queue-group
member could replace a manifest's parent. The web/gateway get read-only mounts
of preview state and no access to `/var/lib/luigi-maintainer`. The gateway needs
no artifact export or source mount; it proxies only the recorded Unix socket.
The installer also pre-creates the `r` socket parent with mode `2750` and the
queue group, and `sources` with mode `0700` and the controller's private group.
The gateway must be able to traverse `r` but never replace entries there.

The gateway identity has no Git, Copilot, release, application tokens, or access
to production data. Its startup rejects the known publisher/agent/release token
variables. The controller has only the protected Git configuration and transient
fetch credential; it rejects gateway, agent, and release keys. Environment checks
are not a substitute for separate OS identities and restricted filesystem access.

## Implemented Parent Integration

The preview controller dispatches `--preview UUID` and `--preview-pending` to
`review_worker.preview_once(config, run_id,
preview_runner=test_preview.start_preview)` in the isolated preview role.
`--preview-pending` must select the oldest eligible `testing` run whose preview
commit does not match its published HEAD. Verify ordering with multiple queued
runs in the installed dispatch. With the sandbox available it is idle when none
qualify; unavailable sandbox checks fail closed.
`--stop-preview` calls the trusted stop controller. Do not use the legacy
all-credentials generation role. The hook reloads
the approved option/evidence, verifies the published branch SHA in a fresh
detached worktree, exports source, and compares the approved tree digest and
image. No candidate tests, imports, package builds, or scripts run on the host.

The hook returns `{ready: True, head_commit, preview_url}` only after a rendered
UDS health check and a matching gateway status. `preview_url` is exclusively
`review.preview_path(run_id)`, a trusted production landing, not a content proxy.
Only the parent hook records that readiness receipt in the review store.

The parent mounts `review_routes`, including the authenticated main-app GET
`/feedback/reviews/{run_id}/preview/`. That landing reloads the review run and
calls `test_preview.create_ticket(run)` to create the signed ticket. It renders
an escaped form with `method="post"`,
`action=preview_origin() + "/session"`, and hidden `run_id` and `ticket` inputs.
The button is **Open test application**. The ticket is body-only; never
use redirects, links, query parameters, telemetry, or request-body logs for it.
Keep the landing no-store, no-referrer, without third-party content; its CSP must
allow that one fixed form destination. Parent mutations still require main-app
authentication and CSRF. Ticket issuance does not approve or publish a review.

The gateway verifies the exact production Origin, a 60-second signed ticket,
current `testing` state, approved head/tree, recorded preview, and active instance.
It atomically consumes a ticket digest in the queue-owned `preview_tickets` table.
At most 256 unexpired tickets exist; no raw ticket is persisted. An opaque run ID
alone authorizes nothing. The gateway returns a trusted continuation link so a
fresh same-site navigation can carry its Strict cookie even after a cross-site
POST. No capability appears in that link or response body.

The host-only `__Host-maintainer_preview` cookie is Secure, HttpOnly, Strict, and
valid for at most one hour and never beyond the active instance lifetime. Every
proxied request checks state/head/instance again. `/session` bodies and gateway
cookies never reach the candidate. Incoming authorization/forwarding headers and
production session cookies are discarded. Only explicit fixture cookie names
pass through; outgoing fixture cookies become host-only, Secure, Strict cookies.
Mutations require the exact preview Origin plus the fixture's own CSRF checks.

## Gateway And Runtime

The standalone module supports `--serve --port 58120` and `--stop`. Help does not
load application configuration. The sole allowed port is deployment-owned and
the listener binds `127.0.0.1`, with access logging and proxy-header trust off.
Provision a separate HTTPS reverse proxy for the configured preview hostname,
preserving its Host and Origin. Do not expose port 58120 directly or map it under
the production hostname. Disable request-body/query logging, enforce a 1 MiB
request limit, and disallow WebSocket upgrades. `/_maintainer/status` is a bounded
readiness endpoint returning only opaque instance/run IDs and commit identity;
the edge may deny it because the controller reaches it directly on loopback.

The [gateway unit](../examples/maintainer-review/luigi-maintainer-gateway.service)
uses its own unprivileged account and root-owned environment file. Share the
gateway key only with the protected production configuration. Do not generate
or store a real key in example files. The separate preview-controller unit has
rootless Podman delegation and no gateway key. Its `KillMode=process` allows
the bounded detached test container to outlive a one-shot controller invocation;
the trusted stop controller, container timeout, and readiness revocation own
cleanup. Validate this lifecycle on Linux before activation. Stopping a systemd
timer or gateway alone is not a stop-preview receipt or verified cleanup.

Use the audited image build context with the reviewed
[preview runner](../scripts/maintainer_preview_app.py) alongside the check runner
and dependency file. Build in a separate secret-free build pool before
provisioning credentials; controlled dependency-download networking is permitted
there, never at candidate runtime. Never install candidate requirements or run
build hooks on the credential-bearing host. The controller chooses the
trusted runner through its fixed command, never a user-supplied entrypoint.
The container keeps network disabled, root filesystem/source read-only, dropped
capabilities, no proxy/environment inheritance, limits, no logs, and a one-hour
runtime timeout. Only its private socket directory is writable on the host;
synthetic databases remain in container tmpfs. The fixture profiles are fixed:
workspace, media, cards, finance. Lifespan hooks and WebSockets are disabled.
The protected controller also supplies a non-secret `--public-origin` argument.
The runner uses it for the media fixture's synthetic absolute cover URLs; those
images stay on the dedicated HTTPS origin instead of pointing at loopback.

The Linux gateway pins each socket inode with O_PATH/O_NOFOLLOW and connects
through its open `/proc/self/fd` reference, preventing a candidate socket-path
swap from redirecting the connection. It never accepts an upstream URL. Bodies
are bounded to 1 MiB, responses to 5 MiB, requests to 30 seconds. Traversal,
external redirects, encoded/compressed responses, and backend error details are
rejected. CSP restricts networking/forms/assets to the preview origin, blocks
frames and service workers, and permits the application's local inline scripts
and styles. Every response is no-store/no-referrer.

Only one active slot exists. Further starts refuse until a protected stop clears
it; there is no automatic takeover. Stop first clears preview/release evidence,
marks a queued/in-progress release for attention, then removes only the recorded
validated container name and its private directories. Uncertain container cleanup
keeps a blocking manifest. Expiry denies browser access and Podman terminates the
container; the protected stop command clears the expired slot and evidence.

## Validation Limits

The focused offline tests use synthetic queue records and mocked Git, Podman,
UDS transport, and fixture contexts. They cover authentication, replay, expiry,
head/instance changes, limits, credential filtering, redirects, source binding,
readiness, role separation, stop revocation, and the parent worker receipt.
They do not establish real Linux rootless Podman, cgroup/SELinux, UDS permissions,
browser cross-site cookie behavior, or reverse-proxy readiness. Those deployment
checks are still required; this change configures no live service or HTTPS origin.
Candidate code is untrusted even inside the sandbox: keep independent CI,
privacy review, protected release policy, and final human approval in place.

The sandbox build-context regression must retain an exact allowlist for the
dependency file and the two reviewed runners. Do not weaken it to permit
arbitrary build-context copies or candidate-provided entrypoints. Role dispatch
implements `--preview UUID`, `--stop-preview`, and `--preview-pending`; verify the
installed commands and synthetic end-to-end flow before enabling the timer.
Ordinary UI review uses the authenticated landing, not per-run server commands.
