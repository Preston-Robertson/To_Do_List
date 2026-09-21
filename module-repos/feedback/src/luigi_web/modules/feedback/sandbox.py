"""Opt-in rootless Podman boundary; never import or execute candidate code here.

Deployment owns this module, the image digest, runtime configuration, and artifact
parent. Candidates must be quiescent, privacy-screened source-only worktrees.
Evidence is advisory, not an attestation: candidate code can falsify test results.
Keep baseline/CI checks and final human approval independent. Nothing in this
module sends email, starts a live preview, or integrates with the worker.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from typing import Mapping
import uuid
import zlib

RUNTIME = "/usr/bin/podman"
GIT = "/usr/bin/git"
IMAGE_PATTERN = r"localhost/luigi-maintainer@sha256:[0-9a-f]{64}"
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_FILES = 10_000
MAX_REPORT_BYTES = 16 * 1024
MAX_PNG_BYTES = 8 * 1024 * 1024
TIMEOUT_SECONDS = 900
SOURCE_SUFFIXES = frozenset({
    ".py", ".html", ".js", ".css", ".json", ".toml", ".txt", ".md",
    ".svg", ".woff2", ".webmanifest", ".sh", ".yml", ".yaml",
})
SOURCE_ROOTS = frozenset({
    "luigi_web", "module-repos", "tests", "scripts", "static", "templates",
    "examples", "docs",
})
ROOT_FILES = frozenset({
    "app.py", "pyproject.toml", "requirements.txt", "requirements-modules.txt",
    "readme.md", "security.md", ".gitignore",
})
PUBLIC_TEMPLATE_PATHS = frozenset({
    "luigi-maintainer.service", "luigi-maintainer.timer", "luigi-web.service",
    "luigi-web-preview.service", "maintainer.env.example",
    "examples/maintainer-review/common.conf",
    "examples/maintainer-review/restricted.conf",
    "examples/maintainer-review/web-queue.conf",
    "examples/maintainer-review/luigi-maintainer-generate.service",
    "examples/maintainer-review/luigi-maintainer-generate.timer",
    "examples/maintainer-review/luigi-maintainer-publish.service",
    "examples/maintainer-review/luigi-maintainer-publish.timer",
    "examples/maintainer-review/luigi-maintainer-refresh.service",
    "examples/maintainer-review/luigi-maintainer-refresh.timer",
    "examples/maintainer-review/luigi-maintainer-notify.service",
    "examples/maintainer-review/luigi-maintainer-notify.timer",
    "examples/maintainer-review/luigi-maintainer-release.service",
    "examples/maintainer-review/luigi-maintainer-release.timer",
    "examples/maintainer-review/luigi-maintainer-preview.service",
    "examples/maintainer-review/luigi-maintainer-preview.timer",
    "examples/maintainer-review/luigi-maintainer-gateway.service",
    "examples/maintainer-review/publish.env.example",
    "examples/maintainer-review/refresh.env.example",
    "examples/maintainer-review/release.env.example",
    "examples/maintainer-review/notify.env.example",
    "examples/maintainer-review/preview.env.example",
    "examples/maintainer-review/gateway.env.example",
    "examples/maintainer-review/web.env.example",
})
PUBLIC_ENV_VALUES = {
    "LUIGI_MAINTAINER_REPOSITORY_URL": {"https://github.com/<owner>/<repository>.git"},
    "LUIGI_MAINTAINER_BASE_BRANCH": {"main"},
    "LUIGI_MAINTAINER_REMOTE": {"origin"},
    "LUIGI_MAINTAINER_GITHUB_TOKEN": {
        "<read-only-repository-token>", "<publication-contents-and-pull-requests-write-token>",
        "<read-only-checks-and-repository-token>",
    },
    "LUIGI_MAINTAINER_COPILOT_TOKEN": {"<copilot-token>"},
    "LUIGI_MAINTAINER_MODEL": {""},
    "LUIGI_MAINTAINER_MAX_AI_CREDITS": {"5"},
    "LUIGI_MAINTAINER_AGENT_TIMEOUT": {"2700"},
    "LUIGI_MAINTAINER_SANDBOX_IMAGE": {"localhost/luigi-maintainer@sha256:<reviewed-image-digest>"},
    "LUIGI_MAINTAINER_REQUIRED_CHECKS": {"offline-regression"},
    "LUIGI_RELEASE_GITHUB_TOKEN": {"<separate-release-token-without-protection-bypass>"},
    "LUIGI_MAINTAINER_SMTP_HOST": {"<smtp-host>"},
    "LUIGI_MAINTAINER_SMTP_PORT": {"587"},
    "LUIGI_MAINTAINER_SMTP_SSL": {"0"},
    "LUIGI_MAINTAINER_SMTP_USERNAME": {"<smtp-username>"},
    "LUIGI_MAINTAINER_SMTP_PASSWORD": {"<smtp-password>"},
    "LUIGI_MAINTAINER_EMAIL_FROM": {"notifications@example.invalid"},
    "LUIGI_MAINTAINER_EMAIL_TO": {"operator@example.invalid"},
    "LUIGI_MAINTAINER_UI_URL": {"https://app.example.invalid"},
    "LUIGI_MAINTAINER_PREVIEW_URL": {"https://preview.example.invalid"},
    "LUIGI_MAINTAINER_PREVIEW_GATEWAY_KEY": {
        "<32-to-64-random-bytes-as-lowercase-hex>", "<same-gateway-key-from-protected-provisioning>",
    },
    "LUIGI_WEB_MAINTAINER_REVIEW_ENABLED": {"0"},
    "LUIGI_WEB_RELEASE_ENABLED": {"0"},
    "LUIGI_WEB_MAINTAINER_DB": {"/var/lib/luigi-maintainer-queue/maintainer.db"},
    "LUIGI_MAINTAINER_ARTIFACT_DIR": {"/var/lib/luigi-maintainer-queue/review-artifacts"},
    "LUIGI_MAINTAINER_PREVIEW_STATE_DIR": {"/var/lib/luigi-maintainer-preview"},
    "LUIGI_WEB_SECURE_COOKIES": {"1"},
}
DENIED_PARTS = frozenset({
    "data", "temp", "tmp", "logs", "artifacts", "screenshots", "backups",
    "node_modules", "venv", "__pycache__", "secrets", "private", "credentials",
})
PREVIEW_KINDS = frozenset({"workspace", "media", "cards", "finance"})
PREVIEW_PAGE_KINDS = {
    "/": "workspace", "/home": "workspace", "/tasks": "workspace",
    "/recurring": "workspace", "/discipline": "workspace", "/calendar": "workspace",
    "/projects": "workspace", "/characters": "workspace", "/modules": "workspace",
    "/games": "media", "/shows": "media", "/movies": "media",
    "/cards": "cards", "/cards/mtg/decks": "cards", "/finance": "finance",
}
CHECK_NAMES = ("tests", "templates", "routes", "whitespace", "screenshots")
CONTAINER_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp/home",
    "TMPDIR": "/tmp", "XDG_CACHE_HOME": "/tmp/cache",
    "XDG_CONFIG_HOME": "/tmp/config", "XDG_DATA_HOME": "/tmp/data",
    "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
    "PYTHONPATH": "/workspace", "PLAYWRIGHT_BROWSERS_PATH": "/opt/browsers",
    "LUIGI_WEB_DATA_DIR": "/tmp/luigi", "LUIGI_WEB_ENV_FILE": "/tmp/absent.env",
    "LUIGI_WEB_UI_TOKEN": "sandbox-synthetic-token",
    "LUIGI_WEB_FINANCE_TOKEN": "sandbox-synthetic-finance-token",
    "LUIGI_WEB_FINANCE_DB": "/tmp/luigi/finance.sqlite3",
    "LUIGI_WEB_CARDS_DB": "/tmp/luigi/cards.sqlite3",
    "LUIGI_WEB_RPG_DB": "/tmp/luigi/characters.sqlite3",
    "LUIGI_WEB_FEEDBACK_DB": "/tmp/luigi/feedback.sqlite3",
    "LUIGI_WEB_MAINTAINER_DB": "/tmp/luigi/maintainer.sqlite3",
    "LUIGI_WEB_MODULES": "tasks,discipline,planning,media,cards,characters,finance,assistant,admin,preview,feedback",
}


class SandboxError(ValueError):
    """A fixed, non-sensitive error safe for a local status display."""


@dataclass(frozen=True)
class Availability:
    available: bool
    reason: str
    required_role: str = "Dedicated unprivileged Linux user; rootless Podman; delegated cgroup v2"


@dataclass(frozen=True)
class SourceSnapshot:
    source_digest: str
    files: int
    bytes: int


@dataclass(frozen=True)
class CheckEvidence:
    name: str
    passed: bool
    exit_code: int
    count: int | None = None


@dataclass(frozen=True)
class ScreenshotEvidence:
    name: str
    sha256: str
    width: int
    height: int


@dataclass(frozen=True)
class TestEvidence:
    candidate_id: str
    passed: bool
    exit_code: int | None
    tests_run: int | None
    source_digest: str
    image: str
    checks: tuple[CheckEvidence, ...] = ()
    screenshots: tuple[ScreenshotEvidence, ...] = ()
    reason: str = ""
    report_trusted: bool = False


def preview_kind_for_path(page_path: str) -> str:
    """Map only fixed public page names, never record URLs or arbitrary routes."""
    if page_path not in PREVIEW_PAGE_KINDS:
        raise SandboxError("No synthetic preview profile is approved for this page.")
    return PREVIEW_PAGE_KINDS[page_path]


def _image(environment: Mapping[str, str]) -> str:
    image = environment.get("LUIGI_MAINTAINER_SANDBOX_IMAGE", "")
    if not re.fullmatch(IMAGE_PATTERN, image):
        raise SandboxError("A pinned localhost sandbox image digest is required.")
    return image


def _runtime_env(environment: Mapping[str, str]) -> dict[str, str]:
    result = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    for key in ("HOME", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        value = environment.get(key)
        if value:
            if not value.startswith("/") or any(char in value for char in "\x00\r\n"):
                raise SandboxError("Invalid trusted runtime directory configuration.")
            result[key] = value
    result.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
    return result


def available(*, environment: Mapping[str, str] | None = None) -> Availability:
    """Inspect only the trusted runtime, never the candidate or a shell."""
    environment = os.environ if environment is None else environment
    try:
        _image(environment)
        runtime_env = _runtime_env(environment)
    except SandboxError as error:
        return Availability(False, str(error))
    if sys.platform != "linux" or getattr(os, "geteuid", lambda: 0)() == 0:
        return Availability(False, "Linux with a non-root runtime identity is required.")
    try:
        result = subprocess.run(
            [RUNTIME, "--remote=false", "info", "--format=json"],
            env=runtime_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=15, check=False,
        )
        if result.returncode or len(result.stdout) > MAX_REPORT_BYTES:
            return Availability(False, "Rootless Podman inspection failed.")
        host = json.loads(result.stdout)["host"]
        if type(host) is not dict or type(host.get("security")) is not dict:
            raise ValueError
        controllers = set(host.get("cgroupControllers", []))
        if (
            host["security"]["rootless"] is not True
            or host["security"].get("seccompEnabled") is not True
            or host["cgroupVersion"] != "v2"
            or not {"cpu", "memory", "pids"}.issubset(controllers)
            or host.get("serviceIsRemote", False) is not False
        ):
            return Availability(False, "Rootless Podman, seccomp and delegated cpu/memory/pids cgroup v2 are required.")
        image = subprocess.run(
            [RUNTIME, "--remote=false", "image", "exists", _image(environment)],
            env=runtime_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=15, check=False,
        )
        if image.returncode:
            return Availability(False, "The pinned sandbox image is not installed locally; runtime downloads are disabled.")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, RecursionError):
        return Availability(False, "Rootless Podman is unavailable or its configuration is unsupported.")
    return Availability(True, "Rootless runtime and pinned local image are available.")


def _source_path(raw: str) -> PurePosixPath | None:
    path = PurePosixPath(raw)
    if (
        not raw or len(raw) > 300 or path.is_absolute() or path.as_posix() != raw
        or any(part in {".", ".."} for part in path.parts)
        or any(ord(char) < 32 or ord(char) > 126 or char in "\\:," for char in raw)
    ):
        raise SandboxError("Invalid candidate source path.")
    if raw in PUBLIC_TEMPLATE_PATHS:
        return path
    parts = tuple(part.casefold() for part in path.parts)
    if any(
        part in DENIED_PARTS or part.startswith(("local", ".env"))
        or any(word in part for word in ("credential", "secret", "token", ".env"))
        or part in {"agents.md", "copilot-instructions.md", "unittest-output.txt"}
        for part in parts
    ):
        return None
    if parts[0] == ".github":
        return path if len(parts) == 3 and parts[1] == "workflows" and path.suffix in {".yml", ".yaml"} else None
    if parts[0] == "module-repos" and len(parts) == 5 and parts[2:4] == (".github", "workflows"):
        return path if path.suffix in {".yml", ".yaml"} else None
    if parts[0] == "module-repos" and len(parts) == 3 and parts[2] == ".gitignore" and not parts[1].startswith("."):
        return path
    if len(parts) == 1:
        return path if parts[0] in ROOT_FILES else None
    if raw == "examples/maintainer-sandbox.Dockerfile":
        return path
    if parts[0] not in SOURCE_ROOTS or any(part.startswith(".") for part in parts):
        return None
    return path if path.suffix.casefold() in SOURCE_SUFFIXES else None


def _validate_public_template(relative: PurePosixPath, content: bytes) -> None:
    """Reject filled-in environment examples, not arbitrary source or comments."""
    if relative.as_posix() not in PUBLIC_TEMPLATE_PATHS or not relative.name.endswith(".env.example"):
        return
    try:
        seen: set[str] = set()
        for line in content.decode("ascii").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or key in seen or value not in PUBLIC_ENV_VALUES.get(key, ()):
                raise ValueError
            seen.add(key)
        if not seen:
            raise ValueError
    except (UnicodeError, ValueError):
        raise SandboxError("Public environment template contains unaudited assignments.") from None


def _regular_bytes(root: Path, relative: PurePosixPath, limit: int) -> bytes:
    """Use no-follow directory descriptors on Linux to close path-swap races."""
    descriptors: list[int] = []
    try:
        if sys.platform == "linux":
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(directory)
            for part in relative.parts[:-1]:
                directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                descriptors.append(directory)
            descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        else:
            probe = root
            for part in relative.parts:
                probe /= part
                if probe.is_symlink() or getattr(probe, "is_junction", lambda: False)():
                    raise SandboxError("Linked files are not permitted.")
            descriptor = os.open(probe, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise SandboxError("Source or artifact is linked, non-regular, or oversized.")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            content = stream.read(limit + 1)
        after = os.fstat(descriptor)
        if len(content) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns,
        ):
            raise SandboxError("Source or artifact changed during inspection.")
        return content
    except OSError:
        raise SandboxError("Source or artifact could not be read safely.") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _plain_directory(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink() or getattr(ancestor, "is_junction", lambda: False)():
            raise SandboxError("Linked directories are not permitted.")
    if not path.is_dir() or any(char in str(path) for char in ",\r\n\x00"):
        raise SandboxError("A plain existing directory is required.")
    return path


def _listed_files(root: Path) -> list[str]:
    try:
        result = subprocess.run(
            [GIT, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
             "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--"],
            cwd=root, env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=30, check=False,
        )
        if result.returncode or len(result.stdout) > MAX_FILES * 301:
            raise SandboxError("Candidate file enumeration failed or exceeded limits.")
        files = result.stdout.decode("utf-8").rstrip("\0").split("\0")
        if len(files) > MAX_FILES:
            raise SandboxError("Candidate contains too many files.")
        return sorted(set(files)) if result.stdout else []
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise SandboxError("Candidate file enumeration failed.") from None


def export_candidate(root: Path, dest: Path) -> SourceSnapshot:
    """Copy tracked edits and nonignored new source, never Git metadata or data.

    Filename filtering is not a content privacy scanner. The controller must
    approve the source surface before calling this and stop candidate writes.
    Public deployment templates are read/export fixtures only, never permission
    for model reads or edits. The controller must pin their reviewed, versioned
    baseline and protect configuration writes. It must screen all source and
    template comments; assignment checks do not certify arbitrary content.
    The destination must be new and outside the candidate worktree.
    """
    root = _plain_directory(root)
    parent = _plain_directory(dest.parent)
    dest = parent / dest.name
    if dest == root or root in dest.parents or dest.exists() or dest.is_symlink():
        raise SandboxError("Export requires a new directory outside the candidate worktree.")
    files = _listed_files(root)
    dest.mkdir(mode=0o700)
    digest = hashlib.sha256()
    total = 0
    count = 0
    seen: set[str] = set()
    try:
        for raw in files:
            relative = _source_path(raw)
            if relative is None:
                continue
            folded = relative.as_posix().casefold()
            if folded in seen:
                raise SandboxError("Case-colliding source paths are not permitted.")
            seen.add(folded)
            source = root.joinpath(*relative.parts)
            if not source.exists() and not source.is_symlink():
                continue
            content = _regular_bytes(root, relative, MAX_FILE_BYTES)
            _validate_public_template(relative, content)
            total += len(content)
            if total > MAX_TOTAL_BYTES:
                raise SandboxError("Candidate source exceeds the total byte limit.")
            target = dest.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(content)
            target.chmod(0o444 if sys.platform == "linux" else 0o644)
            digest.update(raw.encode("ascii") + b"\0" + hashlib.sha256(content).digest())
            count += 1
        if not count:
            raise SandboxError("Candidate contains no exportable source.")
        return SourceSnapshot(digest.hexdigest(), count, total)
    except BaseException:
        shutil.rmtree(dest)
        raise


def _snapshot_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_dir() and not path.is_symlink():
            continue
        relative = PurePosixPath(path.relative_to(root).as_posix())
        content = _regular_bytes(root, relative, MAX_FILE_BYTES)
        digest.update(relative.as_posix().encode("ascii") + b"\0" + hashlib.sha256(content).digest())
    return digest.hexdigest()


def _command(image: str, source: Path, output: Path, name: str, preview_kind: str | None) -> list[str]:
    command = [
        RUNTIME, "--remote=false", "run", "--rm", "--pull=never", "--name", name,
        "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--pids-limit=128", "--memory=2g", "--memory-swap=2g", "--cpus=2",
        "--user=65534:65534", "--userns=keep-id:uid=65534,gid=65534",
        "--ipc=private", "--pid=private", "--uts=private", "--cgroupns=private",
        "--http-proxy=false", "--unsetenv-all", "--log-driver=none",
        "--no-hosts", "--passwd=false", "--hostname=luigi-sandbox", "--image-volume=ignore",
        "--security-opt=mask=/etc/resolv.conf", "--no-healthcheck", "--systemd=false",
        "--sdnotify=ignore", "--read-only-tmpfs=false",
        "--ulimit=core=0:0", "--ulimit=fsize=33554432:33554432",
        "--timeout=900", "--stop-timeout=2",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=1g,mode=1777",
        "--tmpfs=/dev/shm:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        "--mount", f"type=bind,source={source.as_posix()},destination=/workspace,ro,relabel=private,bind-nonrecursive",
        "--mount", f"type=bind,source={output.as_posix()},destination=/output,rw,relabel=private,bind-nonrecursive",
        "--workdir=/workspace", "--entrypoint=/usr/local/bin/python",
    ]
    for key, value in CONTAINER_ENV.items():
        command.extend(("--env", f"{key}={value}"))
    command.extend((image, "-I", "/opt/luigi-tests/check.py"))
    if preview_kind is not None:
        command.extend(("--preview-target", preview_kind))
    return command


def _png_info(content: bytes, expected: tuple[int, int]) -> tuple[int, int]:
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SandboxError("Screenshot is not a bounded PNG.")
    offset = 8
    dimensions = None
    has_pixels = False
    ended = False
    compressed = bytearray()
    channels = 0
    while offset + 12 <= len(content):
        length = struct.unpack_from(">I", content, offset)[0]
        kind = content[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(content) or kind not in {b"IHDR", b"IDAT", b"IEND"}:
            raise SandboxError("Screenshot contains unsupported or truncated PNG chunks.")
        payload = content[offset + 8:end - 4]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != struct.unpack_from(">I", content, end - 4)[0]:
            raise SandboxError("Screenshot CRC is invalid.")
        if kind == b"IHDR":
            if offset != 8 or length != 13 or dimensions is not None:
                raise SandboxError("Screenshot header is invalid.")
            dimensions = struct.unpack_from(">II", payload)
            if dimensions != expected or payload[8:] not in (b"\x08\x02\x00\x00\x00", b"\x08\x06\x00\x00\x00"):
                raise SandboxError("Screenshot dimensions or encoding are invalid.")
            channels = 3 if payload[9] == 2 else 4
        elif kind == b"IDAT":
            if dimensions is None:
                raise SandboxError("Screenshot header is missing.")
            has_pixels = True
            compressed.extend(payload)
        else:
            ended = length == 0 and end == len(content)
            break
        offset = end
    if dimensions is None or not has_pixels or not ended:
        raise SandboxError("Screenshot PNG is incomplete.")
    stride = dimensions[0] * channels + 1
    expected_bytes = stride * dimensions[1]
    try:
        inflater = zlib.decompressobj()
        pixels = inflater.decompress(compressed, expected_bytes + 1)
        if (
            len(pixels) != expected_bytes or not inflater.eof
            or inflater.unconsumed_tail or inflater.unused_data
            or any(pixels[offset] > 4 for offset in range(0, len(pixels), stride))
        ):
            raise SandboxError("Screenshot pixel stream is invalid or oversized.")
    except zlib.error:
        raise SandboxError("Screenshot pixel stream is invalid.") from None
    return dimensions


def _report(output: Path, preview_kind: str | None) -> tuple[tuple[CheckEvidence, ...], tuple[ScreenshotEvidence, ...]]:
    try:
        report = json.loads(_regular_bytes(output, PurePosixPath("report.json"), MAX_REPORT_BYTES))
        names = CHECK_NAMES if preview_kind is not None else CHECK_NAMES[:-1]
        if type(report) is not dict or set(report) != {"version", "checks"} or type(report["version"]) is not int or report["version"] != 1:
            raise ValueError
        rows = report["checks"]
        if type(rows) is not list or len(rows) != len(names):
            raise ValueError
        checks = []
        for name, row in zip(names, rows):
            if type(row) is not dict or set(row) != {"name", "exit_code", "count"} or row["name"] != name:
                raise ValueError
            code, count = row["exit_code"], row["count"]
            if type(code) is not int or not -255 <= code <= 255:
                raise ValueError
            if count is not None and (type(count) is not int or not 0 <= count <= 1_000_000):
                raise ValueError
            if name in {"tests", "templates", "routes"} and code == 0 and not count:
                raise ValueError
            if code == 0 and ((name == "screenshots" and count != 2) or (name == "whitespace" and count != 0)):
                raise ValueError
            checks.append(CheckEvidence(name, code == 0, code, count))
        images = []
        if preview_kind is not None and checks[-1].passed:
            for filename, size in (("desktop.png", (1440, 900)), ("mobile.png", (390, 844))):
                content = _regular_bytes(output, PurePosixPath(filename), MAX_PNG_BYTES)
                width, height = _png_info(content, size)
                images.append(ScreenshotEvidence(filename, hashlib.sha256(content).hexdigest(), width, height))
        return tuple(checks), tuple(images)
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        raise SandboxError("Sandbox report or screenshots are missing, malformed, or oversized.") from None


def run_checks(
    worktree: Path, artifact_root: Path, candidate_id: str, *,
    preview_kind: str | None = None, environment: Mapping[str, str] | None = None,
) -> TestEvidence:
    """Return bounded, untrusted evidence; never accept command/path input.

    Only sanitized technical metadata and validated PNGs survive. Raw container
    stdout/stderr are discarded. PNGs are local human-review artifacts, not LLM
    input. The artifact parent needs a deployment disk quota: memory/cgroup and
    per-file limits do not bound aggregate writes to the private output mount.
    A container shares the host kernel and is not a VM security guarantee.
    """
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", candidate_id):
        raise SandboxError("Invalid candidate identifier.")
    if preview_kind is not None and preview_kind not in PREVIEW_KINDS:
        raise SandboxError("Unknown synthetic preview target.")
    environment = dict(os.environ if environment is None else environment)
    status = available(environment=environment)
    if not status.available:
        return TestEvidence(candidate_id, False, None, None, "", "", reason=status.reason)
    image = _image(environment)
    runtime_env = _runtime_env(environment)
    worktree = _plain_directory(worktree)
    artifact_root = _plain_directory(artifact_root)
    if worktree == artifact_root or worktree in artifact_root.parents:
        raise SandboxError("Artifacts must be outside the candidate worktree.")
    final = artifact_root / candidate_id
    if final.exists() or final.is_symlink():
        raise SandboxError("Candidate artifacts already exist.")
    with tempfile.TemporaryDirectory(prefix="sandbox-", dir=artifact_root) as temporary:
        staging = Path(temporary)
        source, output = staging / "source", staging / "output"
        output.mkdir(mode=0o700)
        snapshot = export_candidate(worktree, source)
        name = "luigi-sandbox-" + uuid.uuid4().hex
        exit_code = None
        reason = ""
        cleaned = False
        try:
            result = subprocess.run(
                _command(image, source, output, name, preview_kind),
                env=runtime_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=TIMEOUT_SECONDS + 30, check=False,
            )
            exit_code = result.returncode
        except subprocess.TimeoutExpired:
            reason = "Sandbox time limit exceeded."
        except (OSError, subprocess.SubprocessError):
            reason = "Sandbox runtime failed; no host execution was attempted."
        finally:
            try:
                cleanup = subprocess.run(
                    [RUNTIME, "--remote=false", "rm", "--force", "--ignore", name],
                    env=runtime_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=15, check=False,
                )
                cleaned = cleanup.returncode == 0
            except (OSError, subprocess.SubprocessError):
                pass
        checks: tuple[CheckEvidence, ...] = ()
        screenshots: tuple[ScreenshotEvidence, ...] = ()
        if not cleaned:
            reason = "Sandbox cleanup could not be confirmed; evidence rejected."
        elif not reason:
            try:
                if _snapshot_digest(source) != snapshot.source_digest:
                    raise SandboxError("Export changed during sandbox execution.")
                with tempfile.TemporaryDirectory(prefix="verify-", dir=artifact_root) as verification:
                    current = export_candidate(worktree, Path(verification) / "source")
                if current.source_digest != snapshot.source_digest:
                    raise SandboxError("Candidate changed during sandbox execution.")
                checks, screenshots = _report(output, preview_kind)
            except SandboxError as error:
                reason = str(error)
        passed = exit_code == 0 and not reason and bool(checks) and all(check.passed for check in checks)
        tests_run = next((check.count for check in checks if check.name == "tests"), None)
        evidence = TestEvidence(candidate_id, passed, exit_code, tests_run, snapshot.source_digest,
                                image, checks, screenshots, reason)
        final.mkdir(mode=0o700)
        try:
            for screenshot in screenshots:
                content = _regular_bytes(output, PurePosixPath(screenshot.name), MAX_PNG_BYTES)
                if hashlib.sha256(content).hexdigest() != screenshot.sha256:
                    raise SandboxError("Screenshot changed during collection.")
                (final / screenshot.name).write_bytes(content)
            from dataclasses import asdict

            (final / "evidence.json").write_text(json.dumps(asdict(evidence), sort_keys=True), encoding="ascii")
        except BaseException:
            shutil.rmtree(final)
            raise
        return evidence


def run_candidate(
    worktree: Path, artifact_root: Path, candidate_id: str, *,
    preview_kind: str = "workspace", environment: Mapping[str, str] | None = None,
) -> TestEvidence:
    return run_checks(worktree, artifact_root, candidate_id, preview_kind=preview_kind, environment=environment)
