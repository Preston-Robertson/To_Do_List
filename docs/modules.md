# Modules

Luigi Web is a modular monolith: one Python/FastAPI host, a shared Jinja/HTMX
shell, and purpose-built feature packages. All existing built-ins remain
bundled and enabled by default. This refactor ships real module folders and
an independently installable example; it does not publish separate feature
repositories or releases.

## Built-in catalog

Each built-in has a manifest and HTTP controller under
`luigi_web/modules/<id>/`, with its feature templates under `templates/` in
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
[../luigi_web/modules/tasks/repository.py](../luigi_web/modules/tasks/repository.py).
The legacy [../luigi_web/db.py](../luigi_web/db.py) import is an identity alias
to that adapter. Discipline therefore still needs shared task storage even
when the Tasks UI is disabled.

## Selecting modules

[../luigi_web/core/module_registry.py](../luigi_web/core/module_registry.py)
resolves the selection in this order:

1. An explicit non-`None` argument to `build_registry(selection=...)`.
2. The `LUIGI_WEB_MODULES` process environment variable, if present.
3. The saved module-selection file, if present.
4. All 11 built-in IDs, never external modules by default.

The saved file is selected by `LUIGI_WEB_MODULES_FILE`, otherwise it is
`modules.json` under `DATA_DIR`. Selection values are comma-separated IDs or
`none`. Unset the environment variable to use the saved selection or defaults;
an empty string and `all` are not supported selection values. Unknown IDs,
duplicates, missing dependencies, dependency cycles, and enabled navigation
collisions are configuration errors. Dependencies are not silently added by
the registry.

For a local Cards and Characters deployment, after installing the host and
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
lifecycle hooks through the registry. Built-in manifests and the bundled
shared task adapter still load for compatibility; built-in templates remain
available to the shared loader. Disabling is not uninstallation or a data
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
saved file. The GUI cannot change the external-module allow-list, install
packages from Git URLs, or broaden its own deployment permissions.

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

[../pyproject.toml](../pyproject.toml) defines `luigi-web` version `0.1.0`,
requires Python 3.11+, and takes runtime dependencies unchanged from
[../requirements.txt](../requirements.txt). From the repository root, in an
activated virtual environment:

```powershell
pip install -r requirements.txt
pip install -e . --no-deps
luigi-web --help
```

The CLI accepts `--host` and `--port`, defaulting to `127.0.0.1:8000`, and runs
one Uvicorn worker. Its help path parses arguments without importing the
application or initializing integrations. It has no module-install or
module-selection command. Existing source deployments retain
`python -m uvicorn app:app --host 127.0.0.1 --port 8080`; the root compatibility
[../app.py](../app.py) remains unchanged.

To build the host and example wheels locally:

```powershell
pip install "setuptools>=68" wheel
pip wheel . --no-deps --no-build-isolation --wheel-dir dist
pip wheel ./examples/example-module --no-deps --no-build-isolation --wheel-dir dist
```

In a separate activated environment, installing the host wheel installs its
declared runtime dependencies as well:

```powershell
pip install ./dist/luigi_web-0.1.0-py3-none-any.whl
luigi-web --help
```

The wheel contains Python packages, the four shared HTML templates in
[../luigi_web/core/templates](../luigi_web/core/templates), feature templates,
and locally served assets. Shared resources are rooted in
[../luigi_web/core/static](../luigi_web/core/static); public `/static/...` URLs
are unchanged. An installed host does not require a source checkout or root
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
For another reviewed repository, deployment installation can pin an immutable
commit. Replace both repository placeholders and the commit placeholder;
this is a syntax example, not a published module repository:

```sh
pip install "git+https://github.com/<owner>/<module-repo>.git@<full-immutable-commit-sha>"
```

Installation, approval, and enablement are separate steps:

1. Install a reviewed package into the same Python environment as the host.
2. Approve its installed entry-point name in `LUIGI_WEB_EXTERNAL_MODULES`.
3. Include its ID in the explicit, environment, or saved selection and restart.

The deployment-only allow-list is a comma-separated list of entry-point names
in the `luigi_web.modules` group. Exactly one installed entry must match each
approved name. Unapproved packages are not loaded by discovery. The built-in
default selection never enables an external package, even after approval.
Before removing approval or uninstalling a module, remove it and any dependent
modules from the next-start selection too; stale selected IDs fail validation.

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
[architecture.md](architecture.md) for the stack decision and remaining shared
service work.