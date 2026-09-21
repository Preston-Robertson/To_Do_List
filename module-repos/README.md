# Feature Repositories

These directories are the canonical implementations, not copies or generated
exports. Each root can become an independent Git repository; no Git repositories,
remotes, tags or releases are created by this extraction.

| Root | Distribution | Other feature distributions required |
| --- | --- | --- |
| tasks | luigi-web-tasks | None |
| discipline | luigi-web-discipline | luigi-web-tasks |
| planning | luigi-web-planning | luigi-web-tasks, luigi-web-discipline |
| media | luigi-web-media | None |
| cards | luigi-web-cards | None |
| characters | luigi-web-characters | None |
| finance | luigi-web-finance | None |
| assistant | luigi-web-assistant | luigi-web-tasks, luigi-web-discipline, luigi-web-media |
| admin | luigi-web-admin | None |
| preview | luigi-web-preview | None |
| feedback | luigi-web-feedback | None |

Every package is version `0.2.0`, requires Python 3.11+, pins the host to
`luigi-web>=0.2,<0.3`, and retains the version-1 manifest API. Direct third-party
requirements live in each `pyproject.toml`. Tasks owns SQLAlchemy/PostgreSQL;
Cards owns ijson; Media owns Sheets/Google authentication; Assistant and Feedback
own Copilot SDK requirements. Admin's optional feature imports do not create
unconditional feature dependencies. Feedback's maintainer has no Preview import.

Discipline needs installed Tasks repository code, not enabled Tasks UI. Python
dependency resolution and the registry's UI requirements are separate contracts.
Planning requires enabled Tasks and Discipline; Assistant also requires Media.

## Development

From the host checkout:

```sh
python -m pip install -r requirements.txt
python -m pip install -r requirements-modules.txt
python -m unittest discover -s tests -p 'test_module_repositories.py' -v
python -m unittest discover -s tests -p 'test_module_packaging.py' -v
python scripts/validate_repo.py
```

`pip install -e .` installs only the host. Checkout namespace discovery makes
these source roots importable without installing them, but does not register
distribution metadata or install their runtime requirements. Use the explicit
editable requirements file for a full development installation. No installation
script executes pip automatically.

The host tests build host, feature and example wheels in temporary directories,
install with `--no-deps --target`, and exercise both host-only and combined
installs away from the checkout. Each feature is also installed with only its
declared feature dependencies. Probes use isolated Python startup without site
initialization, then explicitly add the wheel target and the test environment's
third-party dependency directories; editable `.pth` files and checkout paths
cannot supply missing feature code. Host-only default discovery must find zero
features, and host-only CLI help must not import the application.
Tests use already-installed third-party dependencies; they are not proof of a
fresh dependency resolution from an index. Feature HTTP probes skip lifespan
startup and prohibit database connections, private file reads and network access.
Standalone smoke tests in each root check manifest, syntax and exact wheel files
without importing application code. Domain/browser regressions remain in the
host suite instead of being duplicated into each repository.

## Compatibility And Publication

Cards, Characters and Media now own their legacy CSS/JavaScript under
`module-repos/<id>/src/luigi_web/modules/<id>/static/legacy/{css,js}/`.
Cards owns `cards.css`, `collection.css`, `cards.js` and `collection.js`;
Characters owns `rpg.css` and `rpg.js`; Media owns `media_insights.js`.
These files ship only in their feature wheels, with no duplicate host copies.

The host's `ModuleStaticFiles` compatibility mount preserves their existing
`/static/css/<file>` and `/static/js/<file>` URLs through a fixed filename map
to installed package resources. Missing packages or files return 404. Host
files take precedence, and ordinary StaticFiles traversal and response checks
still apply. Legacy public assets remain available when an installed feature
is disabled; the separate `/module-assets/<id>` mounts remain enabled-only.
Static lookup does not load feature routes, manifests, repositories or hooks.

`app.css`, common shell styles/scripts, fonts, icons and vendor libraries
intentionally remain shared host resources. This is not a split of all CSS
by feature. Transitional application helpers also mean these packages cannot
yet promise compatibility beyond host 0.2.x.

Each root has manual Git publication steps and a tag-triggered wheel workflow.
Workflows build without runtime dependencies, run smoke tests, retain a CI
artifact and publish the wheel plus `SHA256SUMS` to the exact GitHub Release
tag. The tag must match the package version. They use the scoped Actions token
with `contents: write`, refuse asset overwrites, and never publish to an index
or deploy. The host's GUI installer needs public Release assets, not Actions
artifacts. See [../docs/module-repositories.md](../docs/module-repositories.md)
for owner policy, explicit code trust, dependency prerequisites and the separate
worker. Resolve original-source redistribution rights before any
publication: no root license was supplied, and this extraction grants none.
Keep private data, environments, credentials and generated artifacts outside
the source repositories. The host's privacy/security policy continues to apply.