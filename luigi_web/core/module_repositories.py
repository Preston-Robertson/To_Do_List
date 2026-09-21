"""Deployment-approved public repositories and an isolated installation queue.

The caller owns authentication and CSRF. Neither registration nor queuing downloads
or imports code. Policy files are deployment-owned (root, non-writable by group or
others on POSIX; deployment-managed ACLs on Windows). No API writes that policy.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Mapping, TypedDict, cast

from packaging.utils import canonicalize_name, parse_wheel_filename

from ..paths import DATA_DIR

_ID = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
_REPOSITORY = re.compile(r"https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9][A-Za-z0-9_.-]{0,99})\Z")
_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_JOB = re.compile(r"[0-9a-f]{32}\Z")
_BUILTINS = frozenset(("tasks", "discipline", "planning", "media", "cards", "characters", "finance", "assistant", "admin", "preview", "feedback"))
_PREFIXES = {"planning": "/home", "assistant": "/chat", "media": "/games"}
_MAX_POLICY = 1024 * 1024
_SCHEMA = """
CREATE TABLE IF NOT EXISTS repositories (id TEXT PRIMARY KEY, source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, tag TEXT NOT NULL,
    asset TEXT NOT NULL, sha256 TEXT NOT NULL, state TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '', created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS releases (
    module_id TEXT NOT NULL, sha256 TEXT NOT NULL, record TEXT NOT NULL,
    PRIMARY KEY (module_id, sha256)
);
CREATE TABLE IF NOT EXISTS active (module_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS worker (slot INTEGER PRIMARY KEY CHECK(slot = 1), job_id TEXT NOT NULL);
"""


class ModuleInstallError(ValueError):
    """A deliberately public, non-sensitive installer failure."""


class InstallationJob(TypedDict):
    id: str
    source: dict[str, str]
    tag: str
    asset: str
    sha256: str
    state: str
    error: str
    created: str
    restart_required: bool


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ModuleInstallError("Invalid module or repository identifier")
    return value


def normalize_repository(value: object) -> str:
    if not isinstance(value, str):
        raise ModuleInstallError("Use a public HTTPS GitHub repository URL")
    match = _REPOSITORY.fullmatch(value)
    if not match or not _OWNER.fullmatch(match[1]):
        raise ModuleInstallError("Use a public HTTPS GitHub repository URL")
    owner, repository = match.groups()
    repository = repository[:-4] if repository.endswith(".git") else repository
    if not repository or repository.endswith(".") or ".." in repository:
        raise ModuleInstallError("Invalid GitHub repository name")
    return f"https://github.com/{owner.lower()}/{repository.lower()}"


def _source(data: Mapping[str, object]) -> dict[str, str]:
    if not isinstance(data, dict) or set(data) - {"id", "module_id", "moduleid", "repository", "distribution", "package", "prefix"}:
        raise ModuleInstallError("Invalid repository configuration")
    source_id = _identifier(data.get("id"))
    module_id = _identifier(data.get("module_id", data.get("moduleid", source_id)))
    if "module_id" in data and "moduleid" in data and data["module_id"] != data["moduleid"]:
        raise ModuleInstallError("Invalid repository configuration")
    distribution = data.get("distribution", f"luigi-web-{module_id}")
    if not isinstance(distribution, str) or not re.fullmatch(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*", distribution):
        raise ModuleInstallError("Invalid distribution name")
    distribution = str(canonicalize_name(distribution))
    if distribution in {"luigi-web", "pip", "setuptools", "wheel", "packaging"}:
        raise ModuleInstallError("The host and installer cannot be replaced")
    package = f"luigi_web.modules.{module_id}" if module_id in _BUILTINS else f"luigi_web_extensions.{module_id.replace('-', '_')}"
    if data.get("package", package) != package:
        raise ModuleInstallError("Package must use its isolated module namespace")
    if distribution != canonicalize_name(f"luigi-web-{module_id}"):
        raise ModuleInstallError("Modules require their designated luigi-web distribution")
    prefix = _PREFIXES.get(module_id, f"/{module_id}") if module_id in _BUILTINS else f"/extensions/{module_id}"
    if data.get("prefix", prefix) != prefix:
        raise ModuleInstallError("Module prefix must use its designated route namespace")
    return {"id": source_id, "module_id": module_id, "repository": normalize_repository(data.get("repository")),
            "distribution": distribution, "package": package, "prefix": prefix}


def _assert_unique(sources: list[dict[str, str]]) -> None:
    for key in ("id", "module_id", "distribution", "package", "prefix"):
        if len({source[key] for source in sources}) != len(sources):
            raise ModuleInstallError("Repository identities and namespaces must be unique")


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _check_ancestors(path: Path) -> None:
    if any(_is_link(parent) for parent in (path, *path.parents)):
        raise ModuleInstallError("Installer storage must not use symbolic links")


def _storage_path(*parts: str) -> Path:
    root = Path(DATA_DIR).absolute()
    path = root.joinpath(*parts)
    _check_ancestors(path)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ModuleInstallError("Invalid installer storage location")
    return path


@contextmanager
def _database(*, write: bool = False) -> Generator[sqlite3.Connection, None, None]:
    try:
        path = _storage_path("module-installations.db")
        for suffix in ("-journal", "-wal", "-shm"):
            _check_ancestors(Path(str(path) + suffix))
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
        mode = "rwc" if write else "ro"
        with closing(sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, timeout=15)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema=OFF")
            if write:
                connection.executescript(_SCHEMA)
                connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                if write:
                    connection.commit()
            except BaseException:
                connection.rollback()
                raise
    except (OSError, sqlite3.Error):
        raise ModuleInstallError("Installer storage is unavailable") from None


def _database_exists() -> bool:
    return _storage_path("module-installations.db").exists()


def _policy() -> list[dict[str, str]]:
    configured = os.environ.get("LUIGI_WEB_MODULE_REPOSITORIES_FILE", "").strip()
    if not configured:
        return []
    try:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ValueError
        _check_ancestors(path)
        with path.open("rb") as stream:
            status = os.fstat(stream.fileno())
            if not stat.S_ISREG(status.st_mode):
                raise ValueError
            if os.name != "nt" and (status.st_uid != 0 or status.st_mode & 0o022):
                raise ValueError
            raw = stream.read(_MAX_POLICY + 1)
        if len(raw) > _MAX_POLICY:
            raise ValueError
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"version", "sources"} or payload["version"] != 1:
            raise ValueError
        if not isinstance(payload["sources"], list) or len(payload["sources"]) > 256:
            raise ValueError
        sources = [_source(source) for source in payload["sources"]]
        _assert_unique(sources)
        return sources
    except (OSError, ValueError, TypeError, KeyError):
        raise ModuleInstallError("Deployment repository policy is invalid or unreadable") from None


def _owners() -> frozenset[str]:
    configured = os.environ.get("LUIGI_WEB_MODULE_REPOSITORY_OWNERS", "").strip()
    if not configured:
        return frozenset()
    values = [value.strip().lower() for value in configured.split(",")]
    if any(not _OWNER.fullmatch(value) for value in values):
        raise ModuleInstallError("Deployment repository owner policy is invalid")
    return frozenset(values)


def repository_policy() -> dict[str, object]:
    sources, owners = _policy(), _owners()
    return {"enabled": bool(sources or owners), "registration_enabled": bool(owners),
            "owners": sorted(owners), "policy_sources": sources, "restart_required": True}


def list_repositories() -> list[dict[str, str]]:
    sources, owners = _policy(), _owners()
    if _database_exists():
        with _database() as connection:
            rows = connection.execute("SELECT source FROM repositories ORDER BY id").fetchall()
        try:
            for row in rows:
                source = _source(json.loads(row["source"]))
                if source["repository"].split("/")[3] in owners:
                    sources.append(source)
        except (ValueError, TypeError, KeyError):
            raise ModuleInstallError("Repository registry is invalid") from None
    _assert_unique(sources)
    return sorted(sources, key=lambda source: source["id"])


def approved_source(source_id: str) -> dict[str, str]:
    _identifier(source_id)
    for source in list_repositories():
        if source["id"] == source_id:
            return source
    raise ModuleInstallError("Repository is not approved by the deployment")


def register_repository(data: Mapping[str, object], *, trust_confirmed: bool = False) -> dict[str, str]:
    if trust_confirmed is not True:
        raise ModuleInstallError("Explicit trust in executable module code is required")
    source = _source(data)
    fixed, owners = _policy(), _owners()
    if source["repository"].split("/")[3] not in owners:
        raise ModuleInstallError("Repository owner is not approved by the deployment")
    with _database(write=True) as connection:
        try:
            stored = [_source(json.loads(row["source"])) for row in connection.execute("SELECT source FROM repositories")]
        except (ValueError, TypeError, KeyError):
            raise ModuleInstallError("Repository registry is invalid") from None
        if source in stored:
            return source
        _assert_unique(fixed + stored + [source])
        connection.execute("INSERT INTO repositories(id, source) VALUES (?, ?)", (source["id"], _json(source)))
    if approved_source(source["id"]) != source:
        raise ModuleInstallError("Repository registration could not be verified")
    return source


def _artifact(tag: object, asset: object, sha256: object, source: Mapping[str, str]) -> tuple[str, str, str]:
    if not isinstance(tag, str) or not _TAG.fullmatch(tag) or ".." in tag:
        raise ModuleInstallError("Use an exact release tag without path components")
    if not isinstance(asset, str) or len(asset) > 240 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\.whl", asset):
        raise ModuleInstallError("Use a pure Python release wheel filename")
    try:
        name, _, _, tags = parse_wheel_filename(asset)
        if str(name) != source["distribution"] or {str(item) for item in tags} != {"py3-none-any"}:
            raise ValueError
    except ValueError:
        raise ModuleInstallError("Wheel must match the approved pure Python distribution") from None
    if not isinstance(sha256, str) or not _SHA.fullmatch(sha256):
        raise ModuleInstallError("A lowercase SHA256 artifact pin is required")
    return tag, asset, sha256


def queue_install(source_id: str, tag: str, asset: str, sha256: str, trust_confirmed: bool = False) -> InstallationJob:
    if trust_confirmed is not True:
        raise ModuleInstallError("Explicit trust in executable module code is required")
    source = approved_source(source_id)
    tag, asset, sha256 = _artifact(tag, asset, sha256, source)
    job_id = uuid.uuid4().hex
    with _database(write=True) as connection:
        connection.execute("INSERT INTO jobs(id, source, tag, asset, sha256, state, created) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                           (job_id, _json(source), tag, asset, sha256, datetime.now(timezone.utc).isoformat()))
    result = get_job(job_id)
    if (result["source"], result["tag"], result["asset"], result["sha256"]) != (source, tag, asset, sha256):
        raise ModuleInstallError("Installation request could not be verified")
    return result


def _job_record(row: sqlite3.Row) -> InstallationJob:
    try:
        record = dict(row)
        record["source"] = _source(json.loads(record["source"]))
        if not _JOB.fullmatch(record["id"]) or record["state"] not in {"pending", "installing", "installed", "failed"}:
            raise ValueError
        _artifact(record["tag"], record["asset"], record["sha256"], record["source"])
        record["restart_required"] = record["state"] == "installed"
        record["error"] = "Installation failed; the previous release was preserved" if record["error"] else ""
        return cast(InstallationJob, record)
    except (ValueError, KeyError, TypeError):
        raise ModuleInstallError("Installation queue is invalid") from None


def get_job(job_id: str) -> InstallationJob:
    if not isinstance(job_id, str) or not _JOB.fullmatch(job_id):
        raise ModuleInstallError("Invalid installation job identifier")
    if not _database_exists():
        raise ModuleInstallError("Installation job does not exist")
    with _database() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise ModuleInstallError("Installation job does not exist")
    return _job_record(row)


def list_jobs() -> list[InstallationJob]:
    if not _database_exists():
        return []
    with _database() as connection:
        return [_job_record(row) for row in connection.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 200")]


def inventory_records(*, include_inactive: bool = False) -> list[dict[str, object]]:
    if not _database_exists():
        return []
    approved = {source["id"]: source for source in list_repositories()}
    query = "SELECT releases.record FROM releases"
    if not include_inactive:
        query += " JOIN active ON active.module_id = releases.module_id AND active.sha256 = releases.sha256"
    with _database() as connection:
        rows = connection.execute(query).fetchall()
    records = []
    try:
        for row in rows:
            record = json.loads(row["record"])
            source = _source(record["source"])
            if source != approved.get(source["id"]):
                continue
            if not _SHA.fullmatch(record["sha256"]) or record["module_id"] != source["module_id"]:
                raise ValueError
            expected = f"module-packages/{source['module_id']}/{record['sha256']}"
            if record["site_path"] != expected:
                raise ValueError
            _storage_path(*expected.split("/"))
            records.append(record)
    except (ValueError, TypeError, KeyError):
        raise ModuleInstallError("Installed module inventory is invalid") from None
    return records


def installed_modules() -> list[dict[str, object]]:
    from .module_installer import installed_modules as verified_modules

    return verified_modules()


def list_releases(source_id: str) -> list[dict[str, object]]:
    """Return recorded rollback choices, not filesystem paths or verified code."""
    source = approved_source(source_id)
    selected = {(record["module_id"], record["sha256"]) for record in inventory_records()}
    releases = []
    for record in inventory_records(include_inactive=True):
        if record["source"] != source:
            continue
        tag, asset, sha256 = _artifact(record["tag"], record["asset"], record["sha256"], source)
        _, version, _, _ = parse_wheel_filename(asset)
        releases.append({
            "source_id": source_id, "module_id": source["module_id"],
            "tag": tag, "asset": asset, "sha256": sha256, "version": str(version),
            "selected_for_restart": (source["module_id"], sha256) in selected,
        })
    return sorted(releases, key=lambda release: (str(release["version"]), str(release["sha256"])), reverse=True)


def queue_rollback(source_id: str, sha256: str, trust_confirmed: bool = False) -> InstallationJob:
    if trust_confirmed is not True:
        raise ModuleInstallError("Explicit trust in executable module code is required")
    source = approved_source(source_id)
    if not isinstance(sha256, str) or not _SHA.fullmatch(sha256):
        raise ModuleInstallError("Invalid installed artifact pin")
    for record in inventory_records(include_inactive=True):
        if record["source"] == source and record["sha256"] == sha256:
            tag, asset, sha256 = _artifact(record["tag"], record["asset"], sha256, source)
            return queue_install(source_id, tag, asset, sha256, trust_confirmed=True)
    raise ModuleInstallError("Rollback requires a previously installed approved release")


def installation_paths() -> list[str]:
    from .module_installer import installation_paths as verified_paths

    return verified_paths()


installed_paths = installation_paths