"""One-job autonomous maintainer controller.

The controller owns Git and notification credentials. Agent-authored code is
never executed here; full behavioral validation belongs to secret-free PR CI.
"""
from __future__ import annotations

import os
import re
import shutil
import smtplib
import ssl
import subprocess
import sys
import tempfile
import uuid
import logging
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import maintainer
from .maintainer_agent import (
    AgentOutcome,
    PermissionRequiredError,
    SafeWorkspace,
    WorkspacePolicyError,
    run_agent,
)

logger = logging.getLogger("luigi_web.maintainer")

MAX_CHANGED_FILES = 12
MAX_CHANGED_LINES = 1200
REMOTE_RE = re.compile(r"^[A-Za-z0-9._-]{1,50}$")
BRANCH_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,99})*$"
)
PR_URL_RE = re.compile(r"https://[^\s]+/pull/\d+")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b", re.IGNORECASE)
STRONG_SECRET_RE = re.compile(
    r"(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})"
)
LONG_NUMBER_RE = re.compile(r"(?<!\d)\d(?:[ -]?\d){9,}(?!\d)")
PRIVATE_IP_RE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.\d{1,3}\.\d{1,3}|"
    r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
)
MACHINE_PATH_RE = re.compile(r"(?:/home/[A-Za-z0-9._-]+|[A-Za-z]:\\Users\\[^\\\s]+)")
SAFE_EMAIL_DOMAINS = {"example.com", "example.net", "example.org", "example.test"}


class WorkerError(RuntimeError):
    pass


class AttentionRequired(WorkerError):
    pass


@dataclass(frozen=True)
class WorkerConfig:
    state_root: Path
    repository_url: str
    remote: str = "origin"
    base_branch: str = "main"
    agent_timeout: int = 2700

    @property
    def repository_root(self) -> Path:
        return self.state_root / "repository"

    @property
    def worktrees_root(self) -> Path:
        return self.state_root / "worktrees"

    @property
    def copilot_root(self) -> Path:
        return self.state_root / "copilot"

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        state_root = Path(
            os.environ.get("LUIGI_MAINTAINER_STATE_DIR", "/var/lib/luigi-maintainer")
        ).expanduser().resolve()
        queue_root = Path(
            os.environ.get(
                "LUIGI_MAINTAINER_QUEUE_DIR", "/var/lib/luigi-maintainer-queue"
            )
        ).expanduser().resolve()
        repository_url = os.environ.get(
            "LUIGI_MAINTAINER_REPOSITORY_URL", ""
        ).strip()
        remote = os.environ.get("LUIGI_MAINTAINER_REMOTE", "origin").strip()
        base_branch = os.environ.get("LUIGI_MAINTAINER_BASE_BRANCH", "main").strip()
        if not repository_url:
            raise WorkerError("repository URL is not configured")
        _repository_identity(repository_url)
        if not REMOTE_RE.fullmatch(remote):
            raise WorkerError("remote name is invalid")
        _validate_branch(base_branch)
        for name in (
            "LUIGI_MAINTAINER_GITHUB_TOKEN", "LUIGI_MAINTAINER_COPILOT_TOKEN",
        ):
            if not os.environ.get(name, "").strip():
                raise WorkerError(f"{name} is not configured")
        try:
            maintainer.db_path().relative_to(queue_root)
        except ValueError as exc:
            raise WorkerError("maintainer queue must be inside the queue directory") from exc
        try:
            timeout = int(os.environ.get("LUIGI_MAINTAINER_AGENT_TIMEOUT", "2700"))
        except ValueError as exc:
            raise WorkerError("agent timeout is invalid") from exc
        return cls(
            state_root=state_root, repository_url=repository_url,
            remote=remote, base_branch=base_branch,
            agent_timeout=max(60, min(timeout, 7200)),
        )


@dataclass(frozen=True)
class WorktreeContext:
    path: Path
    branch_name: str
    base_commit: str


@dataclass(frozen=True)
class WorkerResult:
    status: str
    job_uuid: str = ""
    pr_url: str = ""
    notification_sent: bool = False


def _validate_branch(value: str) -> str:
    if (
        not BRANCH_RE.fullmatch(value) or ".." in value
        or value.startswith("-") or value.endswith("/") or "\\" in value
    ):
        raise WorkerError("branch name is invalid")
    return value


def _repository_identity(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username
        or parsed.password or parsed.query or parsed.fragment
    ):
        raise WorkerError("repository URL must be credential-free HTTPS")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise WorkerError("repository URL must identify one owner and repository")
    owner, repository = parts
    repository = repository.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+", repository
    ):
        raise WorkerError("repository owner or name is invalid")
    return parsed.hostname, f"{owner}/{repository}"


def _command_env() -> dict[str, str]:
    allowed = {
        "ALL_PROXY", "COMSPEC", "HOME", "HTTPS_PROXY", "HTTP_PROXY", "LANG",
        "LC_ALL", "NO_PROXY", "PATH", "PATHEXT", "SSL_CERT_DIR", "SSL_CERT_FILE",
        "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "WINDIR",
    }
    env = {
        key: value for key, value in os.environ.items()
        if key.upper() in allowed and value
    }
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    })
    return env


def _askpass_path(config: WorkerConfig) -> Path:
    if os.name != "posix":
        raise WorkerError("the installed maintainer worker requires a Linux host")
    config.state_root.mkdir(parents=True, exist_ok=True)
    path = config.state_root / "git-askpass.sh"
    content = """#!/bin/sh
case "$1" in
  *Username*) printf '%s\\n' 'x-access-token' ;;
  *Password*) printf '%s\\n' "$LUIGI_MAINTAINER_GITHUB_TOKEN" ;;
  *) exit 1 ;;
esac
"""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise WorkerError("Git askpass state is invalid")
    if not path.is_file() or path.read_text(encoding="utf-8") != content:
        path.write_text(content, encoding="utf-8")
    path.chmod(0o700)
    return path


def _git_env(config: WorkerConfig) -> dict[str, str]:
    env = _command_env()
    env["GIT_ASKPASS"] = str(_askpass_path(config))
    env["LUIGI_MAINTAINER_GITHUB_TOKEN"] = os.environ[
        "LUIGI_MAINTAINER_GITHUB_TOKEN"
    ]
    return env


def _gh_env(config: WorkerConfig) -> dict[str, str]:
    host, _ = _repository_identity(config.repository_url)
    env = _command_env()
    env["GH_TOKEN"] = os.environ["LUIGI_MAINTAINER_GITHUB_TOKEN"]
    env["GH_HOST"] = host
    return env


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 900,
    check: bool = True,
    label: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise WorkerError(f"{label} tool is unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorkerError(f"{label} timed out") from exc
    if check and result.returncode:
        raise WorkerError(f"{label} failed")
    return result


def _git(
    config: WorkerConfig,
    arguments: list[str],
    *,
    cwd: Path | None = None,
    auth: bool = False,
    check: bool = True,
    label: str,
) -> subprocess.CompletedProcess[str]:
    command = [
        "git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=",
        "-c", "diff.external=", *arguments,
    ]
    env = _git_env(config) if auth else _command_env()
    return _run(command, cwd=cwd, env=env, check=check, label=label)


def prepare_worktree(config: WorkerConfig, job: dict[str, Any]) -> WorktreeContext:
    config.state_root.mkdir(parents=True, exist_ok=True)
    repository = config.repository_root
    if not repository.exists():
        _git(
            config,
            ["clone", "--origin", config.remote, "--no-tags",
             config.repository_url, str(repository)],
            cwd=config.state_root, auth=True, label="repository clone",
        )
    elif not (repository / ".git").is_dir():
        raise WorkerError("isolated repository state is invalid")

    configured_url = _git(
        config, ["remote", "get-url", config.remote], cwd=repository,
        label="remote verification",
    ).stdout.strip()
    if configured_url.rstrip("/") != config.repository_url.rstrip("/"):
        raise WorkerError("isolated repository remote does not match configuration")

    ref = f"refs/remotes/{config.remote}/{config.base_branch}"
    refspec = f"+refs/heads/{config.base_branch}:{ref}"
    _git(
        config, ["fetch", "--prune", "--no-tags", config.remote, refspec],
        cwd=repository, auth=True, label="base branch fetch",
    )
    base_commit = _git(
        config, ["rev-parse", "--verify", ref], cwd=repository,
        label="base commit lookup",
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", base_commit):
        raise WorkerError("base commit is invalid")

    try:
        job_key = uuid.UUID(str(job["uuid"])).hex
        feedback_key = uuid.UUID(str(job["feedback_uuid"])).hex[:12]
    except (ValueError, TypeError, KeyError) as exc:
        raise WorkerError("queued job identifiers are invalid") from exc
    branch_name = _validate_branch(f"automation/feedback-{feedback_key}")
    worktree = config.worktrees_root / job_key
    config.worktrees_root.mkdir(parents=True, exist_ok=True)
    if worktree.exists():
        raise WorkerError("generated worktree already exists")
    branch_exists = _git(
        config, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=repository, check=False, label="branch availability check",
    )
    if branch_exists.returncode == 0:
        raise WorkerError("generated branch already exists")
    _git(config, ["worktree", "prune"], cwd=repository, label="worktree prune")
    _git(
        config, ["worktree", "add", "-b", branch_name, str(worktree), ref],
        cwd=repository, label="worktree creation",
    )
    return WorktreeContext(worktree, branch_name, base_commit.lower())


def cleanup_worktree(config: WorkerConfig, context: WorktreeContext) -> None:
    try:
        context.path.resolve().relative_to(config.worktrees_root.resolve())
    except ValueError:
        return
    _git(
        config, ["worktree", "remove", "--force", str(context.path)],
        cwd=config.repository_root, check=False, label="worktree cleanup",
    )
    if context.path.exists():
        shutil.rmtree(context.path, ignore_errors=True)
    _git(
        config, ["branch", "-D", context.branch_name], cwd=config.repository_root,
        check=False, label="local branch cleanup",
    )
    _git(
        config, ["worktree", "prune"], cwd=config.repository_root,
        check=False, label="worktree prune",
    )


def changed_files(config: WorkerConfig, context: WorktreeContext) -> list[str]:
    tracked = _git(
        config, ["diff", "--name-only", "-z", "HEAD", "--"], cwd=context.path,
        label="changed file inspection",
    ).stdout.split("\0")
    untracked = _git(
        config, ["ls-files", "--others", "--exclude-standard", "-z"],
        cwd=context.path, label="untracked file inspection",
    ).stdout.split("\0")
    return sorted({value for value in tracked + untracked if value}, key=str.casefold)


def _changed_line_count(
    config: WorkerConfig, context: WorktreeContext, files: list[str],
) -> int:
    output = _git(
        config, ["diff", "--numstat", "HEAD", "--"], cwd=context.path,
        label="change size inspection",
    ).stdout
    total = 0
    tracked_names: set[str] = set()
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or "-" in parts[:2]:
            raise AttentionRequired("Binary or uncountable changes require human review.")
        total += int(parts[0]) + int(parts[1])
        tracked_names.add(parts[2])
    for relative in files:
        if relative in tracked_names:
            continue
        path = context.path / relative
        if path.is_file():
            total += len(path.read_text(encoding="utf-8").splitlines())
    return total


def _added_lines(
    config: WorkerConfig, context: WorktreeContext, files: list[str],
) -> list[str]:
    diff = _git(
        config, ["diff", "--unified=0", "--no-ext-diff", "HEAD", "--"],
        cwd=context.path, label="privacy scan diff",
    ).stdout
    lines = [
        line[1:] for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    tracked = set(_git(
        config, ["diff", "--name-only", "HEAD", "--"], cwd=context.path,
        label="tracked file inspection",
    ).stdout.splitlines())
    for relative in files:
        if relative not in tracked:
            lines.extend((context.path / relative).read_text(encoding="utf-8").splitlines())
    return lines


def enforce_change_policy(
    config: WorkerConfig, context: WorktreeContext, files: list[str],
) -> None:
    if len(files) > MAX_CHANGED_FILES:
        raise AttentionRequired(
            f"The change touches {len(files)} files; the automatic limit is {MAX_CHANGED_FILES}."
        )
    workspace = SafeWorkspace(context.path)
    for relative in files:
        try:
            workspace.assert_writable_path(relative)
        except PermissionRequiredError as exc:
            raise AttentionRequired(str(exc)) from exc
        except WorkspacePolicyError as exc:
            raise AttentionRequired(
                f"Human review is required for changed path {relative}."
            ) from exc
    line_count = _changed_line_count(config, context, files)
    if line_count > MAX_CHANGED_LINES:
        raise AttentionRequired(
            f"The change has {line_count} changed lines; the automatic limit is "
            f"{MAX_CHANGED_LINES}."
        )
    for line in _added_lines(config, context, files):
        if "-----BEGIN " in line and "PRIVATE KEY-----" in line:
            raise AttentionRequired("A possible private key was found in the change.")
        if STRONG_SECRET_RE.search(line):
            raise AttentionRequired("A possible credential was found in the change.")
        if LONG_NUMBER_RE.search(line):
            raise AttentionRequired("A long numeric identifier was found in the change.")
        if PRIVATE_IP_RE.search(line) or MACHINE_PATH_RE.search(line):
            raise AttentionRequired("Machine-specific deployment data was found in the change.")
        for match in EMAIL_RE.finditer(line):
            if match.group(1).lower() not in SAFE_EMAIL_DOMAINS:
                raise AttentionRequired("A non-example email address was found in the change.")


def validate_static_changes(
    config: WorkerConfig, context: WorktreeContext, files: list[str],
) -> None:
    _git(
        config, ["diff", "--check", "HEAD", "--"], cwd=context.path,
        label="whitespace validation",
    )
    try:
        SafeWorkspace(context.path).check_syntax(files)
    except (SyntaxError, ValueError, WorkspacePolicyError) as exc:
        raise WorkerError("static syntax validation failed") from exc


def _pr_body(job: dict[str, Any], files: list[str]) -> str:
    frontend = any(
        path.startswith(("static/", "templates/", "luigi_web/core/static/", "luigi_web/core/templates/"))
        for path in files
    )
    manual = (
        "- [ ] Desktop and mobile browser review is required\n" if frontend else ""
    )
    return (
        "## Approved feedback\n\n"
        f"{job['request_text']}\n\n"
        "## Acceptance criteria\n\n"
        f"{job['acceptance_criteria']}\n\n"
        "## Automation boundary\n\n"
        "This draft was produced from a privacy-screened local queue. The maintainer "
        "cannot merge or deploy it. Full tests and template/route checks run in "
        "secret-free pull-request CI.\n\n"
        "## Review\n\n"
        f"{manual}- [ ] CI passes\n- [ ] Human review approves the change\n\n"
        f"Feedback ID: `{job['feedback_uuid']}`\n"
        f"Policy version: `{job['policy_version']}`\n"
    )


def publish_draft_pr(
    config: WorkerConfig,
    context: WorktreeContext,
    job: dict[str, Any],
    files: list[str],
) -> tuple[str, str]:
    _git(config, ["add", "--all", "--"], cwd=context.path, label="change staging")
    _git(
        config, ["diff", "--cached", "--check", "--"], cwd=context.path,
        label="staged change validation",
    )
    title = f"Automated {str(job['category']).lower()} feedback {job['feedback_uuid'][:8]}"
    _git(
        config,
        ["-c", "user.name=Luigi Maintainer",
         "-c", "user.email=luigi-maintainer@example.test",
         "commit", "--no-gpg-sign", "-m", title],
        cwd=context.path, label="automated commit",
    )
    head_commit = _git(
        config, ["rev-parse", "HEAD"], cwd=context.path, label="head commit lookup",
    ).stdout.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", head_commit):
        raise WorkerError("created commit is invalid")
    _git(
        config, ["push", "--set-upstream", config.remote, context.branch_name],
        cwd=context.path, auth=True, label="branch push",
    )

    host, repository = _repository_identity(config.repository_url)
    config.state_root.mkdir(parents=True, exist_ok=True)
    body_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".maintainer-pr-", suffix=".md",
            dir=config.state_root, delete=False,
        ) as handle:
            handle.write(_pr_body(job, files))
            body_path = Path(handle.name)
        body_path.chmod(0o600)
        result = _run(
            ["gh", "pr", "create", "--draft", "--repo", repository,
             "--base", config.base_branch, "--head", context.branch_name,
             "--title", title, "--body-file", str(body_path)],
            cwd=context.path, env=_gh_env(config), label="draft pull request creation",
        )
    finally:
        if body_path:
            body_path.unlink(missing_ok=True)
    urls = PR_URL_RE.findall(result.stdout or "")
    if not urls or urlsplit(urls[-1]).hostname != host:
        raise WorkerError("draft pull request URL is invalid")
    return head_commit, urls[-1]


def send_notification(
    job: dict[str, Any],
    *,
    status: str,
    summary: str = "",
    question: str = "",
    pr_url: str = "",
) -> bool:
    host = os.environ.get("LUIGI_MAINTAINER_SMTP_HOST", "").strip()
    recipient = os.environ.get("LUIGI_MAINTAINER_EMAIL_TO", "").strip()
    sender = os.environ.get("LUIGI_MAINTAINER_EMAIL_FROM", "").strip()
    if not host or not recipient or not sender:
        return False
    try:
        port = int(os.environ.get("LUIGI_MAINTAINER_SMTP_PORT", "587"))
        message = EmailMessage()
        message["Subject"] = f"Luigi maintainer: {status} ({job['uuid'][:8]})"
        message["From"] = sender
        message["To"] = recipient
        lines = [
            f"Status: {status}", f"Job: {job['uuid']}",
            f"Feedback: {job['feedback_uuid']}",
        ]
        clean_summary = maintainer.sanitize_output(summary)
        clean_question = maintainer.sanitize_output(question)
        if clean_summary:
            lines.extend(["", "Summary:", clean_summary])
        if clean_question:
            lines.extend(["", "Approval or clarification needed:", clean_question])
        if pr_url:
            lines.extend(["", f"Draft pull request: {pr_url}"])
        ui_url = os.environ.get("LUIGI_MAINTAINER_UI_URL", "").strip()
        if ui_url:
            parsed = urlsplit(ui_url)
            if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username:
                lines.extend(["", f"Feedback inbox: {ui_url.rstrip('/')}/feedback"])
        message.set_content("\n".join(lines) + "\n")

        username = os.environ.get("LUIGI_MAINTAINER_SMTP_USERNAME", "").strip()
        password = os.environ.get("LUIGI_MAINTAINER_SMTP_PASSWORD", "")
        context = ssl.create_default_context()
        if os.environ.get("LUIGI_MAINTAINER_SMTP_SSL", "0") == "1":
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(
                host, port, timeout=20, context=context,
            )
        else:
            smtp = smtplib.SMTP(host, port, timeout=20)
            smtp.starttls(context=context)
        with smtp:
            if username and password:
                smtp.login(username, password)
            smtp.send_message(message)
        return True
    except Exception:
        logger.error("Maintainer email notification failed for job %s", job["uuid"])
        return False


def _finish(
    job: dict[str, Any],
    *,
    status: str,
    summary: str = "",
    question: str = "",
    error: str = "",
    head_commit: str = "",
    pr_url: str = "",
    notifier: Callable[..., bool] = send_notification,
) -> WorkerResult:
    clean_summary = maintainer.sanitize_output(summary)
    clean_question = maintainer.sanitize_output(question)
    clean_error = maintainer.sanitize_output(error)
    maintainer.finish_job(
        job["uuid"], status=status, summary=clean_summary,
        question=clean_question, error=clean_error,
        head_commit=head_commit, pr_url=pr_url,
    )
    sent = notifier(
        job, status=status, summary=clean_summary,
        question=clean_question, pr_url=pr_url,
    )
    return WorkerResult(status, job["uuid"], pr_url, sent)


def run_once(
    config: WorkerConfig,
    *,
    agent_runner: Callable[..., AgentOutcome] = run_agent,
    preparer: Callable[[WorkerConfig, dict[str, Any]], WorktreeContext] = prepare_worktree,
    change_reader: Callable[[WorkerConfig, WorktreeContext], list[str]] = changed_files,
    policy_checker: Callable[[WorkerConfig, WorktreeContext, list[str]], None] = enforce_change_policy,
    validator: Callable[[WorkerConfig, WorktreeContext, list[str]], None] = validate_static_changes,
    publisher: Callable[[WorkerConfig, WorktreeContext, dict[str, Any], list[str]], tuple[str, str]] = publish_draft_pr,
    cleaner: Callable[[WorkerConfig, WorktreeContext], None] = cleanup_worktree,
    notifier: Callable[..., bool] = send_notification,
) -> WorkerResult:
    maintainer.recover_stale_jobs()
    job = maintainer.claim_next_job()
    if not job:
        return WorkerResult("Idle")
    context: WorktreeContext | None = None
    try:
        context = preparer(config, job)
        maintainer.set_run_context(
            job["uuid"], branch_name=context.branch_name,
            base_commit=context.base_commit,
        )
        outcome = agent_runner(
            context.path, job,
            copilot_home=config.copilot_root / str(job["uuid"]),
            timeout=config.agent_timeout,
        )
        summary = maintainer.sanitize_output(outcome.summary)
        question = maintainer.sanitize_output(outcome.question)
        if outcome.result == "needs_attention":
            return _finish(
                job, status="Needs attention", summary=summary,
                question=question, notifier=notifier,
            )

        files = change_reader(config, context)
        if outcome.result == "no_change":
            if files:
                raise WorkerError("coding session returned an inconsistent no-change result")
            return _finish(
                job, status="No change", summary=summary, notifier=notifier,
            )
        if outcome.result != "ready":
            raise WorkerError("coding session returned an invalid result")
        if not files:
            return _finish(
                job, status="No change", summary=summary, notifier=notifier,
            )
        policy_checker(config, context, files)
        validator(config, context, files)
        head_commit, pr_url = publisher(config, context, job, files)
        return _finish(
            job, status="Draft PR", summary=summary, head_commit=head_commit,
            pr_url=pr_url, notifier=notifier,
        )
    except AttentionRequired as exc:
        return _finish(
            job, status="Needs attention",
            summary="The proposed change crossed an automatic maintenance limit.",
            question=str(exc), notifier=notifier,
        )
    except Exception as exc:  # noqa: BLE001 - convert all worker failures to bounded state
        detail = str(exc) if isinstance(exc, WorkerError) else type(exc).__name__
        return _finish(
            job, status="Failed", summary="The automated maintenance run failed.",
            error=detail, notifier=notifier,
        )
    finally:
        if context is not None:
            try:
                cleaner(config, context)
            except Exception:  # noqa: BLE001 - job outcome is already durable
                logger.error(
                    "Maintainer worktree cleanup failed for job %s", job["uuid"]
                )


def main() -> int:
    try:
        result = run_once(WorkerConfig.from_env())
    except WorkerError as exc:
        print(f"Maintainer configuration error: {exc}", file=sys.stderr)
        return 2
    print(f"Maintainer run status: {result.status}")
    return 1 if result.status == "Failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())