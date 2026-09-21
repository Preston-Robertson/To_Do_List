# Module Repositories

## Source And Release Ownership

The 11 roots in [../module-repos](../module-repos) are canonical source packages,
not generated exports or nested Git repositories. Each contains its own README,
build metadata, tests, and tag-release workflow and can be moved intact into its
own repository. No GitHub repositories, tags, releases, or deployments have been
created by this extraction. Review redistribution rights before public release;
no new license is granted.

The 0.2 host supports Python 3.11+. Feature distributions currently use version
`0.2.0`, require `luigi-web>=0.2,<0.3`, and retain the version-1 module API.
Transitional host helpers prevent treating them as standalone services. Dotted
imports stay `luigi_web.modules.<id>`; checkout implementations now live under
`module-repos/<id>/src/luigi_web/modules/<id>`. Shared styles, fonts, icons and
vendor libraries stay in the host. Feature assets ship in their owning wheels;
the static-resource overlay preserves existing public `/static` URLs.

Each root's README describes authorized Git publication. Pushing `v0.2.0` runs
the workflow only when the tag matches that package's version. The workflow
builds without runtime dependencies, runs package-contract tests, hashes wheels,
retains an Actions artifact, and uses the scoped `GITHUB_TOKEN` to **publish
the wheel and `SHA256SUMS` to the GitHub Release for that exact tag**. It does
not upload to a package index or deploy. Existing release assets are not
overwritten; corrections need a new version and tag. An existing draft is
published only after its asset upload succeeds. A branch-based manual run
produces only a CI artifact. Restrict tag writes and review the build workflow:
an approved owner and checksum do not make an untrusted builder safe.

The installer queries `/repos/<owner>/<repo>/releases/tags/<tag>` and requires
an uploaded wheel asset on a non-draft public release. Actions artifacts,
automatic source archives, branches and source repositories are not install
inputs. `SHA256SUMS` is for the reviewer to obtain the exact wheel's lowercase
SHA-256 pin; the worker compares downloaded bytes to that submitted pin. It
does not treat the checksum file as a signature or automatically trust it.

## Deployment Policy

The authenticated core manager is `/modules/repositories`, independent of Admin;
Admin provides a link when installed. It never accepts shell commands, arbitrary
download URLs, private-repository tokens, or permission changes.

The deployment owner supplies either or both of these process settings:

| Setting | Effect |
| --- | --- |
| `LUIGI_WEB_MODULE_REPOSITORY_OWNERS` | Comma-separated GitHub owners under which authenticated users may register repositories after explicit trust confirmation |
| `LUIGI_WEB_MODULE_REPOSITORIES_FILE` | Absolute path to a read-only version-1 JSON file of fixed approved sources |

With neither setting, installation is disabled. A fixed policy alone does not
enable GUI registration. These are protected deployment settings, not editable
Admin environment fields. The file must be a regular, root-owned file on POSIX,
not group/other writable, with no symbolic-link path components; Windows needs
equivalent deployment-controlled ACLs. Keep it outside application-writable
storage. The host and worker must receive the same policy and `LUIGI_WEB_DATA_DIR`.

[../examples/module-repositories.example.json](../examples/module-repositories.example.json)
shows the schema with placeholders, not live repositories. Each source fixes a
unique ID, module ID, public HTTPS GitHub repository, distribution, package and
route prefix. Feature distributions use `luigi-web-<id>` and their reserved
`luigi_web.modules.<id>` namespace. New external release modules must instead use
`luigi_web_extensions.<id>` (hyphens in IDs become underscores in package names)
and `/extensions/<id>` routes. The general API example package is installed by
ordinary deployment packaging, not this stricter release-wheel path.

## GUI Workflow

1. In `/modules`, save an explicit next-start selection when deliberate
   activation is required. Without a saved/environment selection, all available
   reserved features are selected at startup: all 11 in this checkout, none in
   a host-only installation. New reserved packages may join that default.
2. In `/modules/repositories`, register the public HTTPS repository and module
   identity under an approved owner, confirming trust in executable code. Fixed
   policy sources are already listed and cannot be changed there.
3. Review the source, release builder and assets. Enter the exact tag, matching
   `py3-none-any.whl` filename and lowercase SHA-256. Confirm trust and queue it.
   Browser mutations require authentication, same-origin checks and CSRF; bearer
   clients retain the authenticated bearer exemption from cookie CSRF.
4. The separate worker processes at most one job per invocation. Queued is not
   installed. An Installed result means verified staging for the next restart,
   not that the running process has imported or enabled the module.
5. Restart through the existing deployment mechanism when ready. New packages
   enter the catalog after restart. Enable or disable them through `/modules`,
   save the validated selection, then restart again when a selection changed.
   Already-selected upgrades take effect at the next restart. No installer
   action hot-loads modules or initiates an application restart.

Dependencies must be compatible and already installed in the worker/host Python
environment or available as previously staged feature releases. Install them in
dependency order. The worker does not resolve or fetch missing dependencies.
Discipline requires the Tasks distribution even with the Tasks UI off; Planning
requires enabled Tasks and Discipline; Assistant additionally requires Media.
Selection errors are rejected, not repaired by silently enabling dependencies.
Environment-managed selections remain read-only in the GUI.

## Worker And Storage

The CLI routes directly to the installer without importing the application:

```text
luigi-web module-install --pending
luigi-web module-install --job <job-id>
luigi-web module-install --abandon <job-id> --confirm-worker-stopped
```

These are deployment-worker operations, not instructions to run them in an
application-secret environment. The CLI does not load an environment file.
Use a dedicated worker environment containing only `LUIGI_WEB_DATA_DIR`, the
repository owner/file policy and, if needed, `LUIGI_WEB_MODULES_FILE`. Never load
the main application environment, UI/Finance tokens, PostgreSQL credentials,
Copilot credentials, or a GitHub token into this worker.

The queue/registry is `DATA_DIR/module-installations.db`; immutable releases
are under `DATA_DIR/module-packages/<id>/<sha256>`. Keep these together for
backup/recovery, outside source and public static directories, and without
symbolic links. Both processes need access to the queue and staged resources;
the worker writes them and the host writes registrations/jobs. They contain
installer metadata and code, not domain records. Changing `DATA_DIR` does not
migrate existing storage. Do not hand-edit job leases or staged files.

Downloads are bounded public HTTPS GitHub requests with restricted redirects.
The worker validates hashes, archive paths, namespace, wheel metadata, entry
points and installed dependency compatibility. It rejects native wheels,
startup `.pth` files and package scripts; it does not run Git, pip, builds or
downloaded Python. Failure preserves the previous release. A killed/timed-out
worker retains its lease and is not automatically retried. Only after the
deployment operator has stopped that worker may `--abandon` with explicit
confirmation mark the interrupted job failed; it activates nothing. Queue a
new request after correcting the cause. GUI rollback also queues a previously
installed, still-approved release and requires the worker and a later restart;
it is not a database/schema rollback.

## Optional Systemd Setup

[../examples/module-installer.service](../examples/module-installer.service)
and [../examples/module-installer.timer](../examples/module-installer.timer)
run a oneshot worker as the existing `luigi-web:luigi-web` account every minute.
The account choice allows both processes to use the same private queue/staging
files without granting the worker root. A different dedicated account requires
deliberate shared-directory/group permissions and matching service adaptations;
it is not automatically safer when the host later executes the same code.

These examples assume the host CLI is already installed at
`/opt/luigi-web/.venv/bin/luigi-web` and uses `/opt/luigi-web/data`. They do not
install Python dependencies. The service loads only
`/etc/luigi-web/module-installer.env`, using
[../examples/module-installer.env.example](../examples/module-installer.env.example).
Keep that file `root:luigi-web`, mode `0640`; never reuse the app's credential
file. The worker has only the data directory writable through systemd, and a
five-minute execution limit. A timeout requires the interrupted-job recovery
above, not deletion of the queue database.

The reviewed [../scripts/install_module_worker.sh](../scripts/install_module_worker.sh)
is a one-time deployment helper, not a GUI action. It requires root and explicit
`--install`, installs root-owned units, and creates the credential-free template
only when absent. Existing environment contents are preserved, and unknown
environment keys are refused. New installs leave the timer disabled; existing
timer enablement is left unchanged. With an already provisioned policy,
`--install --enable` explicitly enables/starts only the installer timer.

An operator or deployment provisioner may invoke it later, from the reviewed
checkout, after provisioning the account, host CLI, data directory and the same
protected policy in both processes:

```sh
bash scripts/install_module_worker.sh --install
```

For a prepared environment, adding `--enable` completes the optional timer setup
in that invocation. A freshly created blank template cannot enable the timer.
The helper never prompts for credentials, changes the web service, restarts
Uvicorn, or runs a job directly. The existing
[../luigi-web.service](../luigi-web.service) guidance remains unchanged; a web
service restart does not itself process the installation queue. These files
have not been installed or enabled by adding them to this repository.

## Trust Boundary

Hash pins establish artifact integrity, not authenticity or safe behavior.
Review and trust the publisher, tag controls, build workflow and code. Module
manifests can execute during discovery even when a UI is disabled. On restart,
approved module code runs in the host process with its full access to secrets
and application data. The same-account worker hardening is not a plugin sandbox
or an OS-level promise that secrets cannot be read. Do not install untrusted
code. Real isolation requires a separately secured service/container and an
explicit authenticated interface, which this installer does not provide.

See [modules.md](modules.md) for selection, general entry-point approval and the
versioned API. Offline tests cover wheel/queue policy and HTTP behavior; actual
GitHub publication and systemd deployment require separate operator authorization.