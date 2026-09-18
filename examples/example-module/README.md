# Example module

This is the real, independently installable `luigi-example` package, version
`0.1.0`. It supplies one read-only authenticated status page at
`/extensions/example`, one namespaced template, and a local stylesheet. It
does not access feature records, initialize a database, or contact a provider.
No separate GitHub repository or published release is implied.

## Files and contract

| File | Purpose |
|---|---|
| [pyproject.toml](pyproject.toml) | Python 3.11+, `luigi-web>=0.1,<0.2`, wheel resources, and the `luigi_web.modules` entry point |
| [src/luigi_example/manifest.py](src/luigi_example/manifest.py) | Exports the version-1 `Module` object as `example = luigi_example.manifest:module` |
| [src/luigi_example/routes.py](src/luigi_example/routes.py) | Uses `APIRouter(prefix="/extensions/example")` and the core `create_templates` API |
| [src/luigi_example/templates/status.html](src/luigi_example/templates/status.html) | Renders as `example/status.html` and extends shared `base.html` |
| [src/luigi_example/static/status.css](src/luigi_example/static/status.css) | Served at `/module-assets/example/status.css` only when the module is enabled |

The manifest imports no routes. Approval loads the `Module` object; enablement
imports the router. The host adds authentication and availability checks. The
route gets status from `request.app.state.module_status`, not application
globals. This example declares no lifecycle hooks or mutating routes; new
mutations must follow the host's CSRF and domain-authorization rules.

## Local editable demo

Start in the Luigi Web repository root with a Python 3.11+ virtual environment
activated. These three commands work in PowerShell and POSIX shells:

```powershell
pip install -r requirements.txt
pip install -e . --no-deps
pip install -e ./examples/example-module --no-deps
```

Install the host metadata even when running it from source with `uvicorn
app:app`. The example declares a distribution dependency on `luigi-web`;
`--no-deps` here is appropriate because the host and its runtime dependencies
were installed first, not a substitute for them.

For a local loopback demo, replace the token placeholder before starting.
These commands select only the example, so no PostgreSQL or Finance setup is
needed. Do not reuse a local demo token for production. Copying an env file
does not load it automatically; these commands set process variables directly.

PowerShell:

```powershell
$env:LUIGI_WEB_UI_TOKEN = "<local-ui-token>"
$env:LUIGI_WEB_SECURE_COOKIES = "0"
$env:LUIGI_WEB_EXTERNAL_MODULES = "example"
$env:LUIGI_WEB_MODULES = "example"
luigi-web --host 127.0.0.1 --port 8080
```

POSIX shell:

```sh
export LUIGI_WEB_UI_TOKEN='<local-ui-token>'
export LUIGI_WEB_SECURE_COOKIES=0
export LUIGI_WEB_EXTERNAL_MODULES=example
export LUIGI_WEB_MODULES=example
luigi-web --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080/login`, sign in with your local token, and visit
`http://127.0.0.1:8080/extensions/example`. The page displays the module ID,
package, API version, and running status inside the shared shell. For HTTPS
deployment, use protected environment files and `LUIGI_WEB_SECURE_COOKIES=1`.

`/modules` lists Example as an external package. Selection is read-only while
`LUIGI_WEB_MODULES` is set. To use GUI selection instead, remove that variable
from the deployment environment and restart, but keep
`LUIGI_WEB_EXTERNAL_MODULES=example`. With no saved selection, only the built-ins
are selected by default; approve, select, save, and restart deliberately.

The three states are distinct: installed but unapproved packages are not
discovered; approved but unselected packages have no mounted route or assets;
selected approved packages become available on the next host start. Remove
the example from the next-start selection before removing its approval or
uninstalling it.

## Build and test

From the host repository root:

```powershell
pip install "setuptools>=68" wheel
pip wheel . --no-deps --no-build-isolation --wheel-dir dist
pip wheel ./examples/example-module --no-deps --no-build-isolation --wheel-dir dist
python -m unittest discover -s tests -p test_module_packaging.py -v
```

In a separate activated environment, using those local wheel paths:

```powershell
pip install ./dist/luigi_web-0.1.0-py3-none-any.whl
pip install ./dist/luigi_example-0.1.0-py3-none-any.whl --no-deps
luigi-web --help
```

The host wheel supplies the shared shell; the example wheel contains only its
own Python package, HTML, CSS, and distribution metadata. Reapply the approval
and selection variables above before starting that environment's host.

The host's [../../tests/test_module_packaging.py](../../tests/test_module_packaging.py)
builds both packages in temporary copies, installs into a disposable target,
and verifies the real entry point, application-free CLI help, authentication,
disabled/enabled routes and assets, templates, and writable-data defaults.
The wheel test class is skipped if `setuptools` or `wheel` is missing, so check
the verbose skip result rather than treating every green test run as a wheel
verification. These tests do not install the example into your active host.

## Developing a module

Use this small package as the reference instead of importing
`luigi_web.application` globals. Change the distribution/package names, entry
name, `Module.id`, template namespace, navigation key, and extension prefix
together. Declare packaged resources and compatible host dependencies in the
module's own project metadata.

External modules are trusted code with the host's process secrets and data
privileges, not sandboxed code. Review installation/build code as well as
manifests and routes. For deployment from another repository, use reviewed
`<owner>/<module-repo>` placeholders and an immutable commit pin as described
in [../../docs/modules.md](../../docs/modules.md). The GUI never installs Git
URLs or changes deployment approval. A genuine isolation boundary requires a
separate process or container with an explicit HTTP contract.