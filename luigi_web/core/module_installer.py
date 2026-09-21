"""Wheel-only, out-of-process module staging. Never imports downloaded code.

Run ``python -m luigi_web.core.module_installer --pending`` or call ``main``
from the host CLI. One invocation handles one job. A crashed ``installing`` job
retains the worker lease until explicitly abandoned: it is never retried
automatically. Installation selects
the next-start release only; the running application's import paths are untouched.
"""
from __future__ import annotations

import argparse
import base64
import configparser
import csv
import hashlib
import io
import json
import logging
import os
import re
import stat
import sys
import tempfile
import tomllib
import zipfile
from contextlib import contextmanager
from contextvars import ContextVar
from email.parser import BytesParser
from email.policy import default as email_policy
from importlib import metadata
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

from ..paths import PROJECT_ROOT
from . import module_repositories as repositories
from .module_repositories import InstallationJob, ModuleInstallError

MAX_RELEASE_BYTES = 1024 * 1024
MAX_WHEEL_BYTES = 50 * 1024 * 1024
MAX_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_FILES = 10000
MAX_RATIO = 200
MAX_DEPENDENCIES = 1000
_WHEEL_FILE = ".module-wheel.whl"
_MANIFEST_FILE = ".module-release.json"
_QUIET_HTTP = ContextVar("module_installer_quiet_http", default=False)


class _DependencyError(ModuleInstallError):
    pass


class _DownloadLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _QUIET_HTTP.get()


@contextmanager
def _private_http_logs():
    token = _QUIET_HTTP.set(True)
    log_filter = _DownloadLogFilter()
    names = {"httpx", "httpcore", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks"}
    names.update(name for name in logging.Logger.manager.loggerDict if name.startswith("httpcore."))
    loggers = [logging.getLogger(name) for name in names]
    for logger in loggers:
        logger.addFilter(log_filter)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(log_filter)
        _QUIET_HTTP.reset(token)


def _chunks(response: httpx.Response, maximum: int):
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise ModuleInstallError("Compressed HTTP responses are not accepted")
    declared = response.headers.get("content-length")
    if declared is not None and (not declared.isdecimal() or int(declared) > maximum):
        raise ModuleInstallError("Release download exceeds its size limit")
    total = 0
    for chunk in response.iter_bytes(65536):
        total += len(chunk)
        if total > maximum:
            raise ModuleInstallError("Release download exceeds its size limit")
        yield chunk


def _redirect_allowed(url: str, expected: str, api_asset: str) -> bool:
    if len(url) > 8192 or any(ord(character) < 33 for character in url) or "\\" in url:
        return False
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port is not None or parsed.fragment:
            return False
        if parsed.hostname in {"objects.githubusercontent.com", "release-assets.githubusercontent.com"}:
            return bool(parsed.path.startswith("/") and parsed.path != "/")
        return url == expected or bool(api_asset and url == api_asset)
    except ValueError:
        return False


def _download(job: InstallationJob, destination: Path) -> None:
    source = job["source"]
    owner, repository = source["repository"].split("/")[-2:]
    api_root = f"https://api.github.com/repos/{owner}/{repository}"
    release_url = f"{api_root}/releases/tags/{quote(job['tag'], safe='')}"
    expected = f"{source['repository']}/releases/download/{quote(job['tag'], safe='')}/{quote(job['asset'], safe='')}"
    with _private_http_logs(), httpx.Client(timeout=30, follow_redirects=False, trust_env=False,
                                           headers={"Accept-Encoding": "identity", "User-Agent": "luigi-web-module-installer"}) as client:
        with client.stream("GET", release_url, headers={"Accept": "application/vnd.github+json"}) as response:
            if response.status_code != 200:
                raise ModuleInstallError("Public release metadata is unavailable")
            release = json.loads(b"".join(_chunks(response, MAX_RELEASE_BYTES)))
        if not isinstance(release, dict) or release.get("tag_name") != job["tag"] or release.get("draft") is not False:
            raise ModuleInstallError("Release metadata does not match the requested public release")
        assets = release.get("assets")
        if not isinstance(assets, list):
            raise ModuleInstallError("Release metadata is invalid")
        matching = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == job["asset"]]
        if len(matching) != 1 or matching[0].get("browser_download_url") != expected or matching[0].get("state") != "uploaded":
            raise ModuleInstallError("The exact release wheel is unavailable")
        asset_id = matching[0].get("id")
        api_asset = f"{api_root}/releases/assets/{asset_id}" if type(asset_id) is int and asset_id > 0 else ""
        current = expected
        for redirects in range(4):
            client.cookies.clear()
            with client.stream("GET", current, headers={"Accept": "application/octet-stream"}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location", "")
                    if redirects == 3 or not _redirect_allowed(location, expected, api_asset):
                        raise ModuleInstallError("Release download redirect is not permitted")
                    current = location
                    continue
                if response.status_code != 200:
                    raise ModuleInstallError("Public release wheel is unavailable")
                digest = hashlib.sha256()
                with destination.open("xb") as stream:
                    for chunk in _chunks(response, MAX_WHEEL_BYTES):
                        digest.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if digest.hexdigest() != job["sha256"]:
                    raise ModuleInstallError("Wheel SHA256 does not match the approved pin")
                return
    raise ModuleInstallError("Release download failed")


def _file_digest(path: Path, maximum: int) -> str:
    repositories._check_ancestors(path)
    if not path.is_file() or path.stat().st_size > maximum:
        raise ModuleInstallError("Installed artifact is invalid")
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            total += len(chunk)
            if total > maximum:
                raise ModuleInstallError("Installed artifact exceeds its size limit")
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if info.orig_filename != name or len(name) > 240 or not name.isascii() or "\\" in name or "//" in name:
        raise ModuleInstallError("Wheel contains an unsafe archive path")
    parts = name.rstrip("/").split("/")
    for part in parts:
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part) or part.endswith("."):
            raise ModuleInstallError("Wheel contains an unsafe archive path")
        if part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{number}" for number in range(1, 10)), *(f"LPT{number}" for number in range(1, 10))}:
            raise ModuleInstallError("Wheel contains a reserved filesystem name")
    mode = stat.S_IFMT(info.external_attr >> 16)
    allowed_mode = stat.S_IFDIR if info.is_dir() else stat.S_IFREG
    if mode not in {0, allowed_mode} or info.flag_bits & 1 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise ModuleInstallError("Wheel contains unsupported archive entries")
    if info.file_size > max(1, info.compress_size) * MAX_RATIO or (info.is_dir() and info.file_size):
        raise ModuleInstallError("Wheel compression exceeds its safety limit")
    if any(part.endswith(".data") for part in parts) or Path(name).suffix.lower() in {".pth", ".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib"}:
        raise ModuleInstallError("Wheel contains unsupported installation content")
    return name


def _headers(content: bytes):
    if len(content) > MAX_RELEASE_BYTES:
        raise ModuleInstallError("Wheel metadata exceeds its size limit")
    message = BytesParser(policy=email_policy).parsebytes(content)
    if message.defects:
        raise ModuleInstallError("Wheel metadata is invalid")
    return message


def _one_header(message, key: str) -> str:
    values = message.get_all(key, [])
    if len(values) != 1:
        raise ModuleInstallError("Wheel metadata is missing or ambiguous")
    return str(values[0])


def _requirements(values) -> list[Requirement]:
    requirements = []
    for text in values:
        if len(requirements) >= MAX_DEPENDENCIES or not isinstance(text, str) or len(text) > MAX_RELEASE_BYTES:
            raise _DependencyError("Dependency metadata exceeds its safety limit")
        try:
            requirement = Requirement(text)
        except ValueError:
            raise _DependencyError("Dependency metadata contains an invalid or unsupported requirement") from None
        if requirement.url:
            raise _DependencyError("URL dependencies are not supported")
        if len(requirement.name) > 200 or len(requirement.extras) > MAX_DEPENDENCIES:
            raise _DependencyError("Dependency metadata exceeds its safety limit")
        requirements.append(requirement)
    return requirements


def _provided_extras(values) -> set[str]:
    extras = set()
    for value in values:
        if (not isinstance(value, str) or len(value) > 200
                or not re.fullmatch(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*", value)):
            raise _DependencyError("Dependency metadata declares an invalid extra")
        extras.add(str(canonicalize_name(value)))
        if len(extras) > MAX_DEPENDENCIES:
            raise _DependencyError("Dependency metadata exceeds its safety limit")
    return extras


def _applies(requirement: Requirement, extras: set[str]) -> bool:
    try:
        return requirement.marker is None or any(requirement.marker.evaluate({"extra": extra}) for extra in extras)
    except (ValueError, KeyError):
        raise _DependencyError("Dependency metadata contains an unsupported marker expression") from None


def _validate_wheel(path: Path, source: dict, asset: str, sha256: str) -> tuple[dict, dict[str, bytes]]:
    if _file_digest(path, MAX_WHEEL_BYTES) != sha256:
        raise ModuleInstallError("Wheel SHA256 does not match the approved pin")
    name, version, _, tags = parse_wheel_filename(asset)
    if str(name) != source["distribution"] or {str(tag) for tag in tags} != {"py3-none-any"}:
        raise ModuleInstallError("Wheel filename does not match its approved identity")
    dist_info = f"{str(name).replace('-', '_')}-{version}.dist-info"
    package_path = source["package"].replace(".", "/")
    contents: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if not members or len(members) > MAX_FILES or sum(info.file_size for info in members) > MAX_EXPANDED_BYTES:
            raise ModuleInstallError("Wheel expansion exceeds its safety limit")
        seen: set[str] = set()
        for info in members:
            member = _safe_member(info)
            folded = member.rstrip("/").casefold()
            if folded in seen:
                raise ModuleInstallError("Wheel contains duplicate archive paths")
            seen.add(folded)
            if not (member.startswith(package_path + "/") or member.startswith(dist_info + "/")):
                raise ModuleInstallError("Wheel writes outside its approved package")
            if not info.is_dir():
                contents[member] = archive.read(info)
    required = {f"{dist_info}/{filename}" for filename in ("METADATA", "WHEEL", "RECORD", "entry_points.txt")}
    if not required.issubset(contents) or f"{package_path}/manifest.py" not in contents or f"{package_path}/__init__.py" not in contents:
        raise ModuleInstallError("Wheel is missing module or distribution metadata")
    wheel = _headers(contents[f"{dist_info}/WHEEL"])
    if (_one_header(wheel, "Wheel-Version") != "1.0" or _one_header(wheel, "Root-Is-Purelib") != "true"
            or wheel.get_all("Tag") != ["py3-none-any"]):
        raise ModuleInstallError("Only purelib py3-none-any wheels are supported")
    package_metadata = _headers(contents[f"{dist_info}/METADATA"])
    if (canonicalize_name(_one_header(package_metadata, "Name")) != name
            or Version(_one_header(package_metadata, "Version")) != version):
        raise ModuleInstallError("Wheel metadata does not match its filename")
    python_requires = _one_header(package_metadata, "Requires-Python")
    if not SpecifierSet(python_requires).contains(default_environment()["python_full_version"], prereleases=True):
        raise ModuleInstallError("Wheel does not support this Python version")
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = lambda optionstr: optionstr
    parser.read_string(contents[f"{dist_info}/entry_points.txt"].decode("utf-8"))
    entry_point = f"{source['package']}.manifest:module"
    if (parser.defaults() or parser.sections() != ["luigi_web.modules"]
            or dict(parser["luigi_web.modules"]) != {source["module_id"]: entry_point}):
        raise ModuleInstallError("Wheel must declare only its approved module entry point")
    record_path = f"{dist_info}/RECORD"
    recorded: set[str] = set()
    for row in csv.reader(io.StringIO(contents[record_path].decode("utf-8"), newline=""), strict=True):
        if len(row) != 3 or row[0] in recorded or row[0] not in contents:
            raise ModuleInstallError("Wheel RECORD contains invalid or duplicate paths")
        filename, digest, size = row
        recorded.add(filename)
        if filename == record_path:
            if digest or size:
                raise ModuleInstallError("Wheel RECORD must not hash itself")
            continue
        expected = base64.urlsafe_b64encode(hashlib.sha256(contents[filename]).digest()).rstrip(b"=").decode("ascii")
        if digest != f"sha256={expected}" or size != str(len(contents[filename])):
            raise ModuleInstallError("Wheel RECORD integrity verification failed")
    if recorded != set(contents):
        raise ModuleInstallError("Wheel RECORD does not cover every file")
    requirements = [str(value) for value in package_metadata.get_all("Requires-Dist", [])]
    _requirements(requirements)
    extras = _provided_extras(package_metadata.get_all("Provides-Extra", []))
    info = {"distribution": str(name), "version": str(version), "package": source["package"],
            "entry_point": entry_point, "requires_python": python_requires, "requires_dist": requirements}
    if extras:
        info["provides_extra"] = sorted(extras)
    return info, contents


def _installed_version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        if distribution == "luigi-web":
            try:
                with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
                    raw = stream.read(MAX_RELEASE_BYTES + 1)
                if len(raw) > MAX_RELEASE_BYTES:
                    return None
                project = tomllib.loads(raw.decode("utf-8"))["project"]
                if canonicalize_name(project["name"]) == "luigi-web":
                    return str(Version(project["version"]))
            except (OSError, ValueError, KeyError, TypeError):
                pass
        return None


def _check_dependencies(records: list[dict]) -> None:
    staged = {str(canonicalize_name(record["distribution"])): record for record in records}
    pending = []
    for record in records:
        pending.extend(requirement for requirement in _requirements(record["requires_dist"]) if _applies(requirement, {""}))
    expanded: set[tuple[str, str]] = set()
    checked = 0
    while pending:
        checked += 1
        if checked + len(pending) > MAX_DEPENDENCIES:
            raise _DependencyError("Dependency resolution exceeds its safety limit")
        requirement = pending.pop()
        dependency = str(canonicalize_name(requirement.name))
        version = staged[dependency]["version"] if dependency in staged else _installed_version(dependency)
        if version is None:
            raise _DependencyError(f"Required dependency '{dependency}' is missing; dependencies must be installed separately")
        try:
            compatible = requirement.specifier.contains(Version(version), prereleases=True)
        except ValueError:
            raise _DependencyError(f"Required dependency '{dependency}' has invalid version metadata") from None
        if not compatible:
            raise _DependencyError(f"Required dependency '{dependency}' has an incompatible installed version")
        if not requirement.extras:
            continue
        requested = _provided_extras(requirement.extras)
        contexts = {"", *requested} - {extra for name, extra in expanded if name == dependency}
        if not contexts:
            continue
        if dependency in staged:
            provided = _provided_extras(staged[dependency].get("provides_extra", []))
            requirements = _requirements(staged[dependency]["requires_dist"])
        else:
            try:
                distribution = metadata.distribution(dependency)
            except metadata.PackageNotFoundError:
                raise _DependencyError(f"Required dependency '{dependency}' has no installed extras metadata") from None
            provided = _provided_extras(distribution.metadata.get_all("Provides-Extra", []))
            requirements = _requirements(distribution.requires or [])
        if not requested.issubset(provided):
            raise _DependencyError(f"Required dependency '{dependency}' does not declare the requested extras")
        expanded.update((dependency, extra) for extra in contexts)
        pending.extend(child for child in requirements if _applies(child, contexts))


def _check_candidate(record: dict) -> None:
    others = [_verify_release(item) for item in repositories.inventory_records() if item["module_id"] != record["module_id"]]
    _check_dependencies(others + [record])


def _read_manifest(path: Path) -> dict:
    repositories._check_ancestors(path)
    if not path.is_file():
        raise ModuleInstallError("Installed release manifest is invalid")
    with path.open("rb") as stream:
        content = stream.read(MAX_RELEASE_BYTES + 1)
    if len(content) > MAX_RELEASE_BYTES:
        raise ModuleInstallError("Installed release manifest exceeds its size limit")
    record = json.loads(content)
    if not isinstance(record, dict):
        raise ModuleInstallError("Installed release manifest is invalid")
    return record


def _verify_release(record: dict) -> dict:
    source = repositories._source(record["source"])
    expected_path = f"module-packages/{source['module_id']}/{record['sha256']}"
    if not repositories._SHA.fullmatch(record["sha256"]) or record["site_path"] != expected_path:
        raise ModuleInstallError("Installed release location is invalid")
    root = repositories._storage_path(*expected_path.split("/"))
    manifest = root / _MANIFEST_FILE
    if _read_manifest(manifest) != record:
        raise ModuleInstallError("Installed release manifest verification failed")
    verified, contents = _validate_wheel(root / _WHEEL_FILE, source, record["asset"], record["sha256"])
    if any(record.get(key) != value for key, value in verified.items()):
        raise ModuleInstallError("Installed release identity verification failed")
    for filename, content in contents.items():
        if _file_digest(root / filename, MAX_EXPANDED_BYTES) != hashlib.sha256(content).hexdigest():
            raise ModuleInstallError("Installed release file verification failed")
    expected_files = set(contents) | {_WHEEL_FILE, _MANIFEST_FILE}
    expected_directories = {parent.as_posix() for filename in contents for parent in Path(filename).parents if parent != Path(".")}
    cache_directories = {(Path(filename).parent / "__pycache__").as_posix() for filename in contents if filename.endswith(".py")}
    pending = [root]
    count = 0
    cache_bytes = 0
    while pending:
        with os.scandir(pending.pop()) as children:
            for child in children:
                count += 1
                if count > len(expected_files) + len(expected_directories) + len(cache_directories) + MAX_FILES:
                    raise ModuleInstallError("Installed release exceeds its file limit")
                path = Path(child.path)
                repositories._check_ancestors(path)
                relative = path.relative_to(root).as_posix()
                if child.is_dir(follow_symlinks=False):
                    if relative not in expected_directories | cache_directories:
                        raise ModuleInstallError("Installed release contains unexpected directories")
                    pending.append(path)
                    continue
                if not child.is_file(follow_symlinks=False):
                    raise ModuleInstallError("Installed release contains unsupported files")
                if relative in expected_files:
                    continue
                cache_source = path.parent.parent / (path.name.split(".")[0] + ".py")
                if path.parent.name != "__pycache__" or path.suffix != ".pyc" or cache_source.relative_to(root).as_posix() not in contents:
                    raise ModuleInstallError("Installed release contains unexpected files")
                cache_bytes += child.stat(follow_symlinks=False).st_size
                if cache_bytes > MAX_EXPANDED_BYTES:
                    raise ModuleInstallError("Installed release cache exceeds its size limit")
                path.unlink()
    return record


def installed_modules() -> list[dict]:
    try:
        records = [_verify_release(record) for record in repositories.inventory_records()]
        _check_dependencies(records)
        return records
    except _DependencyError:
        raise
    except Exception:
        raise ModuleInstallError("Installed modules could not be verified") from None


def installation_paths() -> list[str]:
    return [str(repositories._storage_path(*record["site_path"].split("/"))) for record in installed_modules()]


installed_paths = installation_paths


def _claim(job_id: str) -> InstallationJob:
    repositories.get_job(job_id)
    with repositories._database(write=True) as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        job = repositories._job_record(row)
        if job["state"] == "installed":
            return job
        if job["state"] != "pending":
            raise ModuleInstallError("Only pending installation jobs can be processed")
        if connection.execute("SELECT job_id FROM worker WHERE slot = 1").fetchone():
            raise ModuleInstallError("An installation is running or requires explicit recovery")
        connection.execute("INSERT INTO worker(slot, job_id) VALUES (1, ?)", (job_id,))
        connection.execute("UPDATE jobs SET state = 'installing' WHERE id = ?", (job_id,))
    return job


def _assert_approved(job: InstallationJob) -> None:
    if repositories.approved_source(job["source"]["id"]) != job["source"]:
        raise ModuleInstallError("The installation source is no longer approved")


def _publish(job: InstallationJob, record: dict) -> None:
    _verify_release(record)
    _check_candidate(record)
    with repositories._database(write=True) as connection:
        _assert_approved(job)
        lease = connection.execute("SELECT job_id FROM worker WHERE slot = 1").fetchone()
        state = connection.execute("SELECT state FROM jobs WHERE id = ?", (job["id"],)).fetchone()
        if lease is None or lease[0] != job["id"] or state is None or state[0] != "installing":
            raise ModuleInstallError("Installation lease verification failed")
        existing = connection.execute("SELECT record FROM releases WHERE module_id = ? AND sha256 = ?",
                                      (record["module_id"], record["sha256"])).fetchone()
        serialized = repositories._json(record)
        if existing and existing[0] != serialized:
            raise ModuleInstallError("Installed releases are immutable")
        connection.execute("INSERT OR IGNORE INTO releases(module_id, sha256, record) VALUES (?, ?, ?)",
                           (record["module_id"], record["sha256"], serialized))
        connection.execute("INSERT INTO active(module_id, sha256) VALUES (?, ?) ON CONFLICT(module_id) DO UPDATE SET sha256 = excluded.sha256",
                           (record["module_id"], record["sha256"]))
        readback = connection.execute("SELECT sha256 FROM active WHERE module_id = ?", (record["module_id"],)).fetchone()
        if readback is None or readback[0] != record["sha256"]:
            raise ModuleInstallError("Installation activation could not be verified")


def _stage(job: InstallationJob) -> dict:
    source = job["source"]
    parent = repositories._storage_path("module-packages", source["module_id"])
    parent.mkdir(parents=True, exist_ok=True)
    final = repositories._storage_path("module-packages", source["module_id"], job["sha256"])
    if final.exists():
        manifest = final / _MANIFEST_FILE
        record = _verify_release(_read_manifest(manifest))
        if any(record[key] != job[key] for key in ("source", "tag", "asset", "sha256")):
            raise ModuleInstallError("Installed releases are immutable")
    else:
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=parent) as temporary:
            stage = Path(temporary)
            wheel_path = stage / _WHEEL_FILE
            _download(job, wheel_path)
            info, contents = _validate_wheel(wheel_path, source, job["asset"], job["sha256"])
            record = dict(info, module_id=source["module_id"], source=source, sha256=job["sha256"],
                          tag=job["tag"], asset=job["asset"], site_path=f"module-packages/{source['module_id']}/{job['sha256']}")
            _check_candidate(record)
            for filename, content in contents.items():
                destination = stage / filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            with (stage / _MANIFEST_FILE).open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(repositories._json(record))
                stream.flush()
                os.fsync(stream.fileno())
            _assert_approved(job)
            repositories._check_ancestors(final)
            if final.exists():
                raise ModuleInstallError("Installed releases are immutable")
            os.rename(stage, final)
    _check_candidate(record)
    return _verify_release(record)


def apply_job(job_id: str) -> InstallationJob:
    job = _claim(job_id)
    if job["state"] == "installed":
        return job
    previous_sha = None
    publishing = False
    try:
        _assert_approved(job)
        record = _stage(job)
        with repositories._database() as connection:
            previous = connection.execute("SELECT sha256 FROM active WHERE module_id = ?", (record["module_id"],)).fetchone()
            previous_sha = previous[0] if previous else None
        publishing = True
        _publish(job, record)
        if not any(item["sha256"] == job["sha256"] and item["module_id"] == record["module_id"] for item in installed_modules()):
            raise ModuleInstallError("Installation result could not be verified")
        with repositories._database(write=True) as connection:
            _assert_approved(job)
            lease = connection.execute("SELECT job_id FROM worker WHERE slot = 1").fetchone()
            state = connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
            active = connection.execute("SELECT sha256 FROM active WHERE module_id = ?", (record["module_id"],)).fetchone()
            if (lease is None or lease[0] != job_id or state is None or state[0] != "installing"
                    or active is None or active[0] != job["sha256"]):
                raise ModuleInstallError("Installation result could not be verified")
            connection.execute("UPDATE jobs SET state = 'installed', error = '' WHERE id = ?", (job_id,))
            result = repositories._job_record(connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
            if result["state"] != "installed":
                raise ModuleInstallError("Installation result could not be verified")
            connection.execute("DELETE FROM worker WHERE slot = 1 AND job_id = ?", (job_id,))
        return result
    except Exception as error:
        with repositories._database(write=True) as connection:
            lease = connection.execute("SELECT job_id FROM worker WHERE slot = 1").fetchone()
            if lease is not None and lease[0] == job_id:
                if publishing:
                    if previous_sha is None:
                        connection.execute("DELETE FROM active WHERE module_id = ? AND sha256 = ?", (job["source"]["module_id"], job["sha256"]))
                    else:
                        connection.execute("UPDATE active SET sha256 = ? WHERE module_id = ? AND sha256 = ?",
                                           (previous_sha, job["source"]["module_id"], job["sha256"]))
                connection.execute("UPDATE jobs SET state = 'failed', error = 'installation_failed' WHERE id = ? AND state = 'installing'", (job_id,))
                connection.execute("DELETE FROM worker WHERE slot = 1 AND job_id = ?", (job_id,))
        if isinstance(error, _DependencyError):
            raise error from None
        raise ModuleInstallError("Installation failed; inspect deployment policy, artifact pins, and installed dependencies") from None


def apply_pending() -> InstallationJob | None:
    if not repositories._database_exists():
        return None
    with repositories._database() as connection:
        row = connection.execute("SELECT id FROM jobs WHERE state = 'pending' ORDER BY created, id LIMIT 1").fetchone()
    return apply_job(row[0]) if row else None


process_pending = apply_pending


def abandon_job(job_id: str, *, worker_stopped_confirmed: bool = False) -> InstallationJob:
    """Release an interrupted lease without retrying or activating its artifact."""
    if worker_stopped_confirmed is not True:
        raise ModuleInstallError("Confirm the interrupted worker has been stopped")
    repositories.get_job(job_id)
    with repositories._database(write=True) as connection:
        lease = connection.execute("SELECT job_id FROM worker WHERE slot = 1").fetchone()
        state = connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if lease is None or lease[0] != job_id or state is None or state[0] != "installing":
            raise ModuleInstallError("Only the interrupted installation lease can be abandoned")
        connection.execute("UPDATE jobs SET state = 'failed', error = 'installation_abandoned' WHERE id = ?", (job_id,))
        connection.execute("DELETE FROM worker WHERE slot = 1 AND job_id = ?", (job_id,))
    result = repositories.get_job(job_id)
    if result["state"] != "failed":
        raise ModuleInstallError("Installation recovery could not be verified")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage one approved public release wheel for the next application restart.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--job", help="Process a queued installation job ID")
    action.add_argument("--pending", action="store_true", help="Process at most one pending installation")
    action.add_argument("--abandon", help="Fail an interrupted installation and release its worker lease")
    parser.add_argument("--confirm-worker-stopped", action="store_true", help="Confirm the interrupted worker is no longer running")
    arguments = parser.parse_args(argv)
    if arguments.confirm_worker_stopped and not arguments.abandon:
        parser.error("--confirm-worker-stopped requires --abandon")
    try:
        if arguments.abandon:
            abandon_job(arguments.abandon, worker_stopped_confirmed=arguments.confirm_worker_stopped)
            print("Interrupted installation abandoned; no release was activated.")
            return 0
        result = apply_job(arguments.job) if arguments.job else apply_pending()
    except ModuleInstallError:
        print("Module installation failed; the installer did not restart the application.", file=sys.stderr)
        return 1
    print("Module installed; application restart required." if result else "No pending module installation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())