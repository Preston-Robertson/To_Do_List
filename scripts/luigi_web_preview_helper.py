#!/usr/bin/env python3
"""Root-owned helper for one isolated Luigi Web Preview deployment.

Installed as ``/usr/local/sbin/luigi-web-preview``. The web service may invoke
only this parser through a constrained sudoers rule; arbitrary commands, paths,
service names, and refs are never accepted.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

PRODUCTION_ROOT = Path("/opt/luigi-web")
PREVIEW_ROOT = Path("/opt/luigi-web-preview")
PREVIEW_RUNTIME = Path("/opt/luigi-web-preview-runtime")
PREVIEW_DATA = Path("/opt/luigi-web-preview-data")
PRODUCTION_ENV = PRODUCTION_ROOT / "luigi.env"
PREVIEW_ENV = Path("/etc/luigi-web/preview.env")
PREVIEW_UNIT = Path("/etc/systemd/system/luigi-web-preview.service")
SERVICE = "luigi-web-preview.service"
REMOTE = "origin"
BRANCH_FILE = PREVIEW_DATA / "branch"
BRANCH_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,99})*$"
)
DB_KEYS = (
    "LUIGI_WEB_PG_HOST", "LUIGI_WEB_PG_PORT", "LUIGI_WEB_PG_DB",
    "LUIGI_WEB_PG_USER", "LUIGI_WEB_PG_PASSWORD",
)


class HelperError(RuntimeError):
    pass


def emit(payload: dict, *, code: int = 0) -> None:
    print(json.dumps(payload, sort_keys=True))
    raise SystemExit(code)


def run(command: list[str], *, env: dict[str, str] | None = None,
        timeout: int = 900, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout,
        env=env, check=False,
    )
    if check and result.returncode:
        raise HelperError("Preview operation failed; command output is withheld to protect copied data")
    return result


def validate_branch(value: str) -> str:
    branch = str(value or "").strip()
    if (
        not BRANCH_RE.fullmatch(branch) or ".." in branch
        or branch.endswith("/") or branch.startswith("-") or "\\" in branch
    ):
        raise HelperError("invalid remote branch")
    return branch


def parse_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise HelperError(f"required environment file is missing: {path.name}")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value.replace('\\"', '"').replace("\\\\", "\\")
    return values


def db_config(path: Path) -> dict[str, str]:
    values = parse_env(path)
    missing = [key for key in DB_KEYS if not values.get(key)]
    if missing:
        raise HelperError(f"{path.name} is missing Preview database settings")
    for key in ("LUIGI_WEB_PG_DB", "LUIGI_WEB_PG_USER"):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,62}", values[key]):
            raise HelperError("Preview database and role must be plain identifiers")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,253}", values["LUIGI_WEB_PG_HOST"]):
        raise HelperError("Preview requires an explicit PostgreSQL TCP host")
    port = values["LUIGI_WEB_PG_PORT"]
    if not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise HelperError("Invalid PostgreSQL port")
    if any(char in values[key] for key in DB_KEYS for char in ("\r", "\n", "\x00")):
        raise HelperError("Invalid PostgreSQL configuration")
    return values


def isolated_databases() -> tuple[dict[str, str], dict[str, str]]:
    source = db_config(PRODUCTION_ENV)
    target = db_config(PREVIEW_ENV)
    if source["LUIGI_WEB_PG_DB"].casefold() == target["LUIGI_WEB_PG_DB"].casefold():
        raise HelperError("Preview must use a different database name from Production")
    if source["LUIGI_WEB_PG_USER"].casefold() == target["LUIGI_WEB_PG_USER"].casefold():
        raise HelperError("Preview must use a dedicated PostgreSQL role")
    if source["LUIGI_WEB_PG_PASSWORD"] == target["LUIGI_WEB_PG_PASSWORD"]:
        raise HelperError("Preview must not reuse the Production database password")
    return source, target


def pgpass_line(config: dict[str, str]) -> str:
    def escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace(":", "\\:")
    return ":".join(escape(config[key]) for key in DB_KEYS[:4]) + ":" + escape(config[DB_KEYS[4]])


def pg_args(config: dict[str, str]) -> list[str]:
    return [
        "--host", config["LUIGI_WEB_PG_HOST"],
        "--port", config["LUIGI_WEB_PG_PORT"],
        "--username", config["LUIGI_WEB_PG_USER"],
        "--dbname", config["LUIGI_WEB_PG_DB"],
    ]


def with_pgpass(source: dict[str, str], target: dict[str, str]):
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="luigi-preview-pgpass-",
        delete=False,
    )
    try:
        handle.write(pgpass_line(source) + "\n" + pgpass_line(target) + "\n")
        handle.close()
        os.chmod(handle.name, 0o600)
        env = {
            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
            "PGPASSFILE": handle.name, "PGCONNECT_TIMEOUT": "5",
        }
        return Path(handle.name), env
    except Exception:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
        raise


def snapshot_database() -> None:
    source, target = isolated_databases()
    pgpass, env = with_pgpass(source, target)
    try:
        verify_target(source, target, env)
        with tempfile.TemporaryDirectory(prefix="luigi-preview-snapshot-") as directory:
            dump_path = Path(directory) / "snapshot.dump"
            run(["pg_dump", *pg_args(source), "--no-password", "--format=custom",
                 "--schema=public", "--no-owner", "--no-privileges", "--file", str(dump_path)],
                env={**env, "PGOPTIONS": "-c default_transaction_read_only=on"})
            verify_target(source, target, env)
            run(["pg_restore", *pg_args(target), "--no-password", "--no-owner", "--no-privileges",
                 "--clean", "--if-exists", "--single-transaction", "--exit-on-error",
                 str(dump_path)], env=env)
    finally:
        pgpass.unlink(missing_ok=True)


def clear_preview_database() -> None:
    source, target = isolated_databases()
    pgpass, env = with_pgpass(target, target)
    try:
        verify_target(source, target, env)
        run(["psql", *pg_args(target), "--no-password", "--no-psqlrc", "--set", "ON_ERROR_STOP=1", "--command",
             "BEGIN; DROP SCHEMA public CASCADE; CREATE SCHEMA public; COMMIT;"], env=env)
    finally:
        pgpass.unlink(missing_ok=True)


def verify_target(source: dict[str, str], target: dict[str, str], env: dict[str, str]) -> None:
    query = """
        SELECT json_build_object(
            'database', current_database(), 'role', current_user, 'session_role', session_user,
            'unsafe_role', EXISTS (
                SELECT 1 FROM pg_roles
                                WHERE (rolname = current_user
                                             AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls))
                                     OR (rolname <> current_user AND pg_has_role(current_user, oid, 'MEMBER'))
            ),
            'source_access', EXISTS (
                SELECT 1 FROM pg_database
                WHERE datname = '%s' AND has_database_privilege(current_user, oid, 'CONNECT')
            )
        )
    """ % source["LUIGI_WEB_PG_DB"]
    result = run(["psql", *pg_args(target), "--no-password", "--no-psqlrc", "--tuples-only",
                  "--no-align", "--set", "ON_ERROR_STOP=1", "--command", query],
                 env={**env, "PGOPTIONS": "-c default_transaction_read_only=on"}, timeout=15)
    try:
        metadata = json.loads(result.stdout) if len(result.stdout) <= 4096 else None
    except (ValueError, TypeError):
        metadata = None
    expected = {"database": target["LUIGI_WEB_PG_DB"], "role": target["LUIGI_WEB_PG_USER"],
                "session_role": target["LUIGI_WEB_PG_USER"], "unsafe_role": False, "source_access": False}
    if metadata != expected:
        raise HelperError("Preview PostgreSQL isolation check failed: require a dedicated role without elevated privileges, memberships, or Production CONNECT access")


def verify_database() -> None:
    source, target = isolated_databases()
    pgpass, env = with_pgpass(target, target)
    try:
        verify_target(source, target, env)
    finally:
        pgpass.unlink(missing_ok=True)


def remote_branches() -> list[str]:
    output = run([
        "git", "-C", str(PRODUCTION_ROOT), "for-each-ref",
        "--format=%(refname:strip=3)", f"refs/remotes/{REMOTE}",
    ]).stdout.splitlines()
    branches = []
    for value in output:
        try:
            branch = validate_branch(value)
        except HelperError:
            continue
        if branch != "HEAD":
            branches.append(branch)
    return sorted(set(branches), key=str.casefold)


def assert_remote_branch(branch: str) -> None:
    if branch not in remote_branches():
        raise HelperError("branch is not an allow-listed remote ref")


def ensure_layout() -> None:
    if not PREVIEW_ENV.is_file() or not PREVIEW_UNIT.is_file():
        raise HelperError("Preview environment or service unit is not installed")
    verify_database()
    PREVIEW_DATA.mkdir(parents=True, exist_ok=True)
    user = pwd.getpwnam("luigi-web-preview")
    os.chown(PREVIEW_DATA, user.pw_uid, user.pw_gid)


def install_dependencies() -> None:
    python = PREVIEW_RUNTIME / ".venv" / "bin" / "python"
    if not python.exists():
        PREVIEW_RUNTIME.mkdir(parents=True, exist_ok=True)
        run(["/usr/bin/python3", "-m", "venv", str(PREVIEW_RUNTIME / ".venv")])
    run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
            "--no-cache-dir", "-r", str(PRODUCTION_ROOT / "requirements.txt")])


def service_active() -> bool:
    return run(["systemctl", "is-active", "--quiet", SERVICE], check=False).returncode == 0


def status() -> dict:
    branch = BRANCH_FILE.read_text(encoding="utf-8").strip() if BRANCH_FILE.is_file() else ""
    commit = ""
    dirty = False
    if (PREVIEW_ROOT / ".git").exists():
        commit = run(["git", "-C", str(PREVIEW_ROOT), "rev-parse", "--short", "HEAD"], check=False).stdout.strip()
        dirty = bool(run(["git", "-C", str(PREVIEW_ROOT), "status", "--porcelain"], check=False).stdout.strip())
    preview_env = parse_env(PREVIEW_ENV) if PREVIEW_ENV.is_file() else {}
    port = preview_env.get("LUIGI_WEB_PORT", "")
    healthy = False
    if service_active() and port.isdigit():
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
                healthy = response.status == 200
        except Exception:
            healthy = False
    return {
        "configured": PREVIEW_ENV.is_file() and PREVIEW_UNIT.is_file(),
        "exists": PREVIEW_ROOT.exists(),
        "branch": branch,
        "commit": commit,
        "dirty": dirty,
        "service_active": service_active(),
        "healthy": healthy,
        "port": int(port) if port.isdigit() else None,
    }


def create(branch: str) -> dict:
    branch = validate_branch(branch)
    ensure_layout()
    run(["git", "-C", str(PRODUCTION_ROOT), "fetch", REMOTE, "--prune"])
    assert_remote_branch(branch)
    if PREVIEW_ROOT.exists():
        raise HelperError("Preview worktree already exists")
    run(["git", "-C", str(PRODUCTION_ROOT), "worktree", "add", "--detach",
         str(PREVIEW_ROOT), f"refs/remotes/{REMOTE}/{branch}"])
    BRANCH_FILE.write_text(branch + "\n", encoding="utf-8")
    install_dependencies()
    snapshot_database()
    run(["systemctl", "enable", "--now", SERVICE])
    return status()


def update() -> dict:
    ensure_layout()
    if not PREVIEW_ROOT.exists() or not BRANCH_FILE.is_file():
        raise HelperError("Preview does not exist")
    if run(["git", "-C", str(PREVIEW_ROOT), "status", "--porcelain"]).stdout.strip():
        raise HelperError("Preview worktree has local changes")
    branch = validate_branch(BRANCH_FILE.read_text(encoding="utf-8").strip())
    run(["git", "-C", str(PRODUCTION_ROOT), "fetch", REMOTE, "--prune"])
    assert_remote_branch(branch)
    run(["systemctl", "stop", SERVICE], check=False)
    run(["git", "-C", str(PREVIEW_ROOT), "checkout", "--detach",
         f"refs/remotes/{REMOTE}/{branch}"])
    install_dependencies()
    snapshot_database()
    run(["systemctl", "restart", SERVICE])
    return status()


def restart() -> dict:
    verify_database()
    if not PREVIEW_ROOT.exists():
        raise HelperError("Preview does not exist")
    run(["systemctl", "restart", SERVICE])
    return status()


def remove() -> dict:
    run(["systemctl", "disable", "--now", SERVICE], check=False)
    if PREVIEW_ENV.is_file():
        clear_preview_database()
    if PREVIEW_ROOT.exists():
        run(["git", "-C", str(PRODUCTION_ROOT), "worktree", "remove", "--force",
             str(PREVIEW_ROOT)])
    shutil.rmtree(PREVIEW_RUNTIME, ignore_errors=True)
    shutil.rmtree(PREVIEW_DATA, ignore_errors=True)
    return status()


def main() -> None:
    if os.geteuid() != 0:
        raise HelperError("Preview helper must run as root")
    if len(sys.argv) not in {2, 3}:
        raise HelperError("expected one allow-listed operation")
    action = sys.argv[1]
    if action == "verify" and len(sys.argv) == 2:
        verify_database()
        emit({"ok": True})
    if action == "status" and len(sys.argv) == 2:
        emit({"ok": True, **status()})
    if action == "branches" and len(sys.argv) == 2:
        emit({"ok": True, "configured": True, "branches": remote_branches()})
    if action == "create" and len(sys.argv) == 3:
        emit({"ok": True, **create(sys.argv[2])})
    if action == "update" and len(sys.argv) == 2:
        emit({"ok": True, **update()})
    if action == "restart" and len(sys.argv) == 2:
        emit({"ok": True, **restart()})
    if action == "remove" and len(sys.argv) == 2:
        emit({"ok": True, **remove()})
    raise HelperError("invalid operation")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - safe metadata-only response
        detail = str(exc) if isinstance(exc, HelperError) else "Preview operation failed"
        emit({"ok": False, "configured": True, "error": detail[:500]}, code=1)