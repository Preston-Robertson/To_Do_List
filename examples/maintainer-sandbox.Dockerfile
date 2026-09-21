# Build only from an audited release in a separate secret-free build context.
# Required build argument: PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:<digest>.
# Supply only this file, reviewed requirements.txt, and the reviewed runner.
# Never build from the candidate worktree or use COPY . / ADD remote sources.
# Build-time networking installs dependencies/browser; runtime never pulls.
# Record the resulting image as localhost/luigi-maintainer@sha256:<digest> in
# deployment-owned LUIGI_MAINTAINER_SANDBOX_IMAGE. A mutable tag is rejected.
# Use a dedicated rootless Linux identity with delegated cpu/memory/pids cgroup
# v2, seccomp, no sensitive mounts.conf entries, and quota-limited artifact disk.
# Runtime HOME is needed by Podman but is never mounted or passed to candidates.
# Rootless containers share the kernel; they are not an adversarial VM boundary.
# Results/screenshots are untrusted, local, human-review-only evidence. Keep
# unchanged baseline tests/independent CI, protect this runner/image/policy from
# candidate edits, compare source_digest before approval, and never auto-approve.
# This example neither enables the worker nor sends email nor deploys previews.
# Whitespace checks cover the exported source, not just a Git diff. Offline or
# read-only-incompatible tests fail closed; do not weaken sandbox flags to pass.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/browsers

COPY requirements.txt /opt/luigi-tests/requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends git nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir -r /opt/luigi-tests/requirements.txt \
       'setuptools>=68' wheel 'packaging>=24,<27' 'tzdata>=2024.1' \
       'playwright==1.55.0' 'Pillow==11.3.0' \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /opt/browsers

COPY scripts/maintainer_sandbox_check.py /opt/luigi-tests/check.py
COPY --chmod=0444 scripts/maintainer_preview_app.py /opt/luigi-tests/preview.py
RUN chmod 0444 /opt/luigi-tests/check.py /opt/luigi-tests/requirements.txt \
    && mkdir /workspace /output && chmod 0755 /workspace /output

USER 65534:65534
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/python", "-I", "/opt/luigi-tests/check.py"]
