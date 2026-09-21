# Modules

Luigi Web is a modular monolith: one Python/FastAPI host, a shared Jinja/HTMX
shell, and independently packaged features. The 11 canonical source roots in
[../module-repos](../module-repos) can each become a separate GitHub repository.
The combined checkout exposes all 11 through fixed namespace paths and enables
available reserved features by default. An installed host alone exposes zero
features. No remote repositories or releases have been created by extraction.

## Built-in catalog

Each built-in has a manifest and HTTP controller under
`module-repos/<id>/src/luigi_web/modules/<id>/`, with feature templates under `templates/` in
that package. The current catalog is:

| ID | Responsibility | Required module IDs |
|---|---|---|
| `tasks` | Tasks, recurring work, follow-ups, reminders, and Undo | None |
| `discipline` | Habits, streaks, and shared completion history | None |
| `planning` | Home, projects, calendar, and Daily/Weekly Review | `tasks`, `discipline` |
| `media` | Games, shows, and Game'N'Watch integrations | None |
| `cards` | Trading-card catalogs, decks, collections, and prices | None |
| `characters` | Character sheets, level states, and rules references | None |
| `finance` | Separately unlocked accounts, budgets, and reports | None |
| `assistant` | Bounded task and media chat tools | `tasks`, `discipline`, `media` |
| `admin` | Host settings, integration health, and maintenance | None |
| `preview` | Separately unlocked deployment-preview controls | None |
| `feedback` | Local feedback and explicitly approved maintenance requests | None |

These are selection dependencies, not a claim of complete domain independence.
Tasks and Discipline both use LuigiBot's shared PostgreSQL schema through
[../module-repos/tasks/src/luigi_web/modules/tasks/repository.py](../module-repos/tasks/src/luigi_web/modules/tasks/repository.py).
The legacy [../luigi_web/db.py](../luigi_web/db.py) import is an identity alias
to that adapter. Discipline therefore still needs shared task storage even
when the Tasks UI is disabled. It declares `luigi-web-tasks` as a Python package
dependency; package dependencies and enabled-module dependencies are distinct.

## Selecting modules

[../luigi_web/core/module_registry.py](../luigi_web/core/module_registry.py)
resolves the selection in this order:

1. An explicit non-`None` argument to `build_registry(selection=...)`.
2. The `LUIGI_WEB_MODULES` process environment variable, if present.
3. The saved module-selection file, if present.
4. Available reserved feature IDs, never external modules by default. This is
  all 11 in the combined checkout and none in a host-only wheel installation.

The saved file is selected by `LUIGI_WEB_MODULES_FILE`, otherwise it is
`modules.json` under `DATA_DIR`. Selection values are comma-separated IDs or
`none`. Unset the environment variable to use the saved selection or defaults;
an empty string and `all` are not supported selection values. Unknown IDs,
duplicates, missing dependencies, dependency cycles, and enabled navigation
collisions are configuration errors. Dependencies are not silently added by
the registry.

For a local Cards and Characters deployment, after installing the host, both
feature distributions and their declared dependencies, and
supplying `LUIGI_WEB_UI_TOKEN` through your protected process environment:

```powershell
$env:LUIGI_WEB_MODULES = "cards,characters"
luigi-web --host 127.0.0.1 --port 8080
```

The equivalent POSIX shell command is:

```sh
LUIGI_WEB_MODULES=cards,characters luigi-web --host 127.0.0.1 --port 8080
```

Neither selection requires PostgreSQL. Cards-only also avoids task and Finance
initialization. `LUIGI_WEB_MODULES=none` starts the authenticated core shell and
module manager without PostgreSQL startup or feature lifecycle hooks. Supplying
module selection does not supply authentication credentials or load an env file.

Disabling a module does not delete its data, import its router, or run its
lifecycle hooks through the registry. Available manifests can still load for
discovery, and installed feature templates remain available to the shared
loader. The Tasks adapter is optional host-side but required by Discipline.
Disabling is not uninstallation or a data
security boundary. Existing domain-specific authentication and storage rules
remain in force.

### Authenticated module manager

`/modules` lists the built-ins and deployment-approved installed external
modules. It provides filtering, dependency information, current status,
selection toggles, reset of unsaved changes, and a save action. Installation
alone does not put an unapproved external package in this catalog.

A save validates the complete selection, writes it atomically, and verifies
the saved value. It takes effect on the **next process restart**. The current
routers, navigation, and lifecycle state are not hot-reloaded, and saving does
not restart or deploy the service. The page distinguishes pending selection
from running state. Missing dependencies are rejected by the server.

If `LUIGI_WEB_MODULES` is present, selection is deployment-managed: the GUI is
read-only for selection, and the save operation rejects changes. Remove that
variable in deployment configuration and restart to return control to the
saved file. The GUI cannot change deployment allow-lists or execute Git/pip.
The separate core `/modules/repositories` manager registers policy-approved
public repositories and queues exact release wheels, with authentication,
same-origin browser CSRF and explicit code-trust confirmation. Its worker stages
code for a later restart; see [module-repositories.md](module-repositories.md).
Save an explicit selection before staging a new reserved feature if it must not
join the default available-feature selection on restart.

The version-1 file format, implemented in
[../luigi_web/core/module_settings.py](../luigi_web/core/module_settings.py), is:

```json
{
  "version": 1,
  "enabled": ["cards", "characters"]
}
```

An empty `enabled` list means core-only. If this file is consulted, an invalid
or unreadable file is a configuration error, not permission to enable every
module. Keep instance selections out of version control.

## Packaging and paths

[../pyproject.toml](../pyproject.toml) defines the `luigi-web` version `0.2.0`
host for Python 3.11+. Each feature has its own distribution and dependencies,
with `luigi-web>=0.2,<0.3` compatibility and the existing version-1 manifest API.
Transitional host helpers mean these packages are not standalone servers or
compatible with arbitrary host versions. From the combined checkout, in an
activated virtual environment:

```powershell
pip install -r requirements.txt
pip install -r requirements-modules.txt
luigi-web --help
```

The CLI accepts `--host` and `--port`, defaulting to `127.0.0.1:8000`, and runs
one Uvicorn worker. Its help path parses arguments without importing the
application or initializing integrations. `luigi-web module-install --pending`
processes at most one queued release in a separate deployment worker; `--job`
targets one job and `--abandon` requires `--confirm-worker-stopped`. That CLI path
also avoids importing the application. Selection remains in `/modules` or
deployment configuration. Existing source deployments retain
`python -m uvicorn app:app --host 127.0.0.1 --port 8080`; the root compatibility
[../app.py](../app.py) remains unchanged.

To build the host, a feature, and example wheels locally:

```powershell
pip install "setuptools>=68" wheel
pip wheel . --no-deps --no-build-isolation --wheel-dir dist
pip wheel ./module-repos/cards --no-deps --no-build-isolation --wheel-dir dist
pip wheel ./examples/example-module --no-deps --no-build-isolation --wheel-dir dist
```

In a separate activated environment, installing the host wheel installs its
declared runtime dependencies as well:

```powershell
pip install ./dist/luigi_web-0.2.0-py3-none-any.whl
pip install ./dist/luigi_web_cards-0.2.0-py3-none-any.whl
luigi-web --help
```

The host wheel contains core Python packages, shared HTML templates in
[../luigi_web/core/templates](../luigi_web/core/templates), and shell assets.
Each feature wheel owns its Python package, templates and browser assets.
Shared resources are rooted in
[../luigi_web/core/static](../luigi_web/core/static); public `/static/...` URLs
are unchanged through the legacy package-resource overlay. Missing feature
assets return 404 rather than falling back to copies in the host wheel.
An installed host does not require a source checkout or root
template/static directories. Use `luigi-web` or
`uvicorn luigi_web.application:app` outside a checkout, not `uvicorn app:app`.

[../luigi_web/paths.py](../luigi_web/paths.py) separates installed resources
from writable defaults. `LUIGI_WEB_DATA_DIR` optionally overrides `DATA_DIR`.
Without it, a source checkout uses its existing `data/` directory; an installed
package uses per-user storage:

| Platform | Installed default |
|---|---|
| Windows | `LOCALAPPDATA/luigi-web`, falling back to `~/AppData/Local/luigi-web` |
| macOS | `~/Library/Application Support/luigi-web` |
| Other platforms | `XDG_DATA_HOME/luigi-web`, falling back to `~/.local/share/luigi-web` |

Existing per-feature database/path overrides still apply. Without a data-dir
override, source checkouts also retain the legacy repository-root locations
for task metadata and Game'N'Watch credentials; installed defaults place those
under `DATA_DIR`. Setting a new directory does not migrate existing data.
Service deployments should explicitly choose protected writable storage,
separate from installed packages and public static assets.

## External packages

External modules are **trusted executable Python code, not sandboxed plugins**.
Package installation can execute build code. Approval loads a manifest in the
host process, even if its router is not selected. Enabled code has the host
process's access to secrets and application data. Authentication injection,
route namespaces, and separate database conventions do not remove those
privileges. Untrusted code needs a separate process or container and an
explicit, authenticated HTTP contract; that sandbox is not supplied here.

The working local package is documented in
[../examples/example-module/README.md](../examples/example-module/README.md).
For public release-wheel installation through the GUI, use the separately
documented [repository policy and worker](module-repositories.md). That worker
only parses and stages bounded pure-Python wheels: no source builds, Git, pip,
package scripts, or imports of downloaded code. It does not install dependencies.
The independently installable example uses its own package namespace; it
demonstrates the general API, not the installer's stricter
`luigi_web_extensions.<id>` namespace contract for new external release modules.

For ordinary deployment-installed external packages, installation, approval,
and enablement are separate steps:

1. Install a reviewed package into the same Python environment as the host.
2. Approve its installed entry-point name in `LUIGI_WEB_EXTERNAL_MODULES`.
3. Include its ID in the explicit, environment, or saved selection and restart.

The deployment-only allow-list is a comma-separated list of entry-point names
in the `luigi_web.modules` group. Exactly one installed entry must match each
approved name. Unapproved packages are not loaded by discovery. The built-in
default selection never enables an external package, even after approval.
Before removing approval or uninstalling a module, remove it and any dependent
modules from the next-start selection too; stale selected IDs fail validation.
Repository-staged external entries instead derive discovery approval from their
still-approved repository records; they are never enabled by default. Both
paths load trusted code on restart, not sandboxed extensions.

### Version-1 contract

- The entry point exports a `Module` object from
  `luigi_web.core.module_registry`, not a factory. Its `api_version` is `1`,
  `id` matches the approved entry name, and `package` names its importable
  resource package. IDs start with a lowercase letter and contain at most
  48 lowercase letters, digits, underscores, or hyphens.
- `router` is a lazy `package.module:attribute` reference to an `APIRouter`.
  External routes must be under `/extensions/{id}`. Only HTTP API routes are
  accepted; use manifest hooks, not router startup/shutdown handlers.
- Duplicate module IDs, enabled navigation keys/paths, conflicting route
  registrations, and claims on `/`, `/login`, `/logout`, `/healthz`, `/static`,
  `/modules`, `/module-assets`, or `/command-palette` are rejected. Navigation
  uses local absolute paths without query strings or fragments and existing
  locally served Lucide icon names.
- The host adds main authentication and module-availability dependencies to
  each router. Cookie-authenticated mutations remain subject to the global
  CSRF check; only a valid bearer credential gives the bearer exemption.
  Additional feature-specific authorization remains the module's job.
- Use `create_templates(package="luigi_example", namespace="example")` from
  `luigi_web.core.templating`. Render `example/status.html`, which can extend
  shared `base.html`. The core supplies navigation and module-state context.
  Package external templates under their own namespace rather than replacing
  the core shell. An optional `template_setup` reference receives each
  registered Jinja environment for synchronous filter/global setup. Create and
  retain the templates object at module import time, as the example does:
  setup is applied during host construction, not to per-request factories.
- Ship assets inside the resource package's `static/` directory and declare
  them in package data. The host mounts an enabled package's assets at
  `/module-assets/{id}/...`, separate from `/static`. These are public static
  files: never place records, credentials, or other private content there.
- Optional `startup` and `shutdown` references receive the FastAPI `app`.
  Async hooks are awaited; synchronous hooks run in a thread pool, with any
  returned awaitable also awaited. Dependencies start first. A startup-hook
  failure marks that module unavailable; dependent modules are blocked and
  their routes return 503 while unrelated modules can continue. Shutdown
  cleanup runs in reverse order, including cleanup for attempted startup,
  and logs failures without exposing the underlying exception.

Invalid manifests, router imports, route conflicts, and template/asset setup
errors can prevent host construction. Lifecycle failure isolation is not a
promise that arbitrary package code can fail safely at every stage.

Use the core APIs and `request.app.state` for new external modules, not
`luigi_web.application` globals. The host's legacy helpers and `sys.modules`
identity shims preserve existing imports, monkeypatches, and process-local
state for already-existing modules. They are transitional compatibility APIs,
not a new external-module service contract.

## Validation

Run from the repository root in a development environment without production
credentials:

```powershell
pip install "setuptools>=68" wheel
python -m unittest discover -s tests -p "test_module*.py" -v
python -m unittest discover -s tests -v
python scripts/validate_repo.py
git diff --check
```

[../tests/test_module_packaging.py](../tests/test_module_packaging.py) builds
host and example wheels from disposable source copies, installs them into a
temporary target, and probes entry-point approval, selection, authentication,
packaged templates/assets, CLI help, and installed writable defaults. It does
not install the example into the working environment. `WheelPackagingTests`
is skipped if either `setuptools` or `wheel` is unavailable. Read the skip
summary: `OK (skipped=...)` alone does not establish that wheel checks ran.

[../scripts/validate_repo.py](../scripts/validate_repo.py) compiles the shared
template loader's HTML and checks unique mounted method/path registrations.
It imports the host without starting its lifespan; route coverage follows the
selected modules. For full built-in route coverage, explicitly select all IDs
from the catalog above in a clean validation process. It does not by itself
discover every external template namespace; the example's packaging tests
compile and serve its namespaced page separately.

Frontend changes additionally require browser review at 1440x900 and 390x844,
including remembered appearance modes and reduced-motion behavior. Do not use
production records or private screenshots for validation. See
[../README.md](../README.md) for repository-wide validation and synthetic preview
commands.