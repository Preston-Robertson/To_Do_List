"""Constrained client for the root-owned Preview deployment helper."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_HELPER = Path("/usr/local/sbin/luigi-web-preview")
MUTATING_ACTIONS = frozenset({"create", "update", "restart", "remove"})
_BRANCH_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,99})*$"
)


def helper_path() -> Path:
    configured = os.environ.get("LUIGI_WEB_PREVIEW_HELPER", "").strip()
    return Path(configured) if configured else DEFAULT_HELPER


def helper_available() -> bool:
    path = helper_path()
    return path.is_file() and os.access(path, os.X_OK)


def validate_branch(branch: str) -> str:
    branch = str(branch or "").strip()
    if (
        not _BRANCH_RE.fullmatch(branch)
        or ".." in branch
        or branch.endswith("/")
        or branch.startswith("-")
        or "\\" in branch
    ):
        raise ValueError("invalid remote branch name")
    return branch


def _run(arguments: list[str], *, timeout: int = 900) -> dict[str, Any]:
    path = helper_path()
    if not helper_available():
        return {
            "configured": False,
            "ok": False,
            "error": "Preview helper is not installed",
        }
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    command = [str(path), *arguments]
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() != 0:
        command = ["/usr/bin/sudo", "-n", str(path), *arguments]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Preview helper failed ({type(exc).__name__})") from exc
    output = (completed.stdout or "").strip()
    try:
        payload = json.loads(output) if output else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError("Preview helper returned an invalid response") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Preview helper returned an invalid response")
    payload.setdefault("configured", True)
    payload.setdefault("ok", completed.returncode == 0)
    if completed.returncode != 0:
        raise RuntimeError(str(payload.get("error") or "Preview operation failed"))
    return payload


def status() -> dict[str, Any]:
    return _run(["status"], timeout=15)


def branches() -> list[str]:
    payload = _run(["branches"], timeout=30)
    values = payload.get("branches") or []
    if not isinstance(values, list):
        raise RuntimeError("Preview helper returned invalid branches")
    return [validate_branch(str(value)) for value in values]


def mutate(action: str, *, branch: str = "") -> dict[str, Any]:
    if action not in MUTATING_ACTIONS:
        raise ValueError("invalid Preview operation")
    arguments = [action]
    if action == "create":
        arguments.append(validate_branch(branch))
    elif branch:
        raise ValueError("branch is only accepted for Preview creation")
    return _run(arguments)