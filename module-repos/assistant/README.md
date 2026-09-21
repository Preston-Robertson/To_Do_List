# luigi-web-assistant

Canonical source for this independently buildable Luigi Web feature lives in
`src/luigi_web/modules/assistant`. Python 3.11+ and `luigi-web>=0.2,<0.3` are
required. Exact runtime and cross-feature requirements are in `pyproject.toml`.
The entry point uses the existing version-1 module API. Installing a dependency
does not enable its UI; the host controls module selection separately.

This is not a standalone server. Transitional host helpers, the shared shell,
fonts/icons/vendor libraries still come from the compatible host. Feature
templates and `static` resources belong to their owning packages, including
legacy assets served through the host's unchanged `/static` URL overlay.
Do not add parent `luigi_web/__init__.py` or
`luigi_web/modules/__init__.py` files here: the host owns that namespace.

## Build And Test

```sh
python -m pip install "setuptools>=68" wheel build
python -m build --wheel --no-isolation
python -m unittest discover -s tests -v
```

Building and the local smoke tests do not import the host or access application
data. Tests check the manifest, Python syntax and exact wheel resources. Domain
and browser integration tests remain in the Luigi Web host test suite for now;
they are not duplicated here. Install the matching host and declared dependencies
before runtime integration. In the combined checkout, use the host's explicit
`requirements-modules.txt` for editable development; installing just the host
does not install any feature distributions.

## Separate Repository

After moving this whole directory to its own checkout and reviewing the source
and redistribution permissions, these are manual publication steps:

```sh
git init -b main
git add src tests pyproject.toml README.md .gitignore .github
git commit -m "Extract Luigi Web feature"
git remote add origin <repository-url>
git push -u origin main
git tag v0.2.0
git push origin v0.2.0
```

Pushing a `v*` tag is an explicit publication action. The tag must be `v` plus
the version in `pyproject.toml` (currently `v0.2.0`). The workflow builds only a
wheel, runs smoke tests, retains a CI artifact, and **publishes the wheel and
`SHA256SUMS` as GitHub Release assets on that exact tag**. It uses the scoped
Actions `GITHUB_TOKEN` with `contents: write`, not deployment credentials.
Existing assets are not overwritten; publish fixes under a new package version
and tag. A manual workflow run on a branch builds an artifact without publishing.
There is no package-index upload or deployment.

For the host's `/modules/repositories` manager, first publish to a public GitHub
repository under a deployment-approved owner. Register the HTTPS repository and
module identity, explicitly confirm code trust, then queue the exact release tag,
wheel filename, and lowercase SHA-256 from `SHA256SUMS`. Actions artifacts alone
are not installable: the worker queries the public Releases API. Review the
builder and source; a checksum proves integrity, not trust. The separate worker
requires compatible dependencies already installed and only stages for the next
host restart. Selection is managed separately in `/modules`; there is no hot load.

## Privacy And License

Never include `.env`, `LOCAL_*`, application data, credentials, database files,
raw feedback or personal records in source, fixtures, logs or release artifacts.
Use synthetic fixtures only. Runtime data belongs outside this checkout.

No new license is granted by this extraction. The original source did not supply
a root license file; confirm redistribution rights and applicable original terms
before publishing. Existing third-party notices remain applicable in their
owning packages. Do not invent or replace license terms.