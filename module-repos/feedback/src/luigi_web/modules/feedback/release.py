"""Fixed, controller-only GitHub publication and human-approved version tags.

The parent owns claims, durable receipts, preview readiness and attention states.
Never call this module from generated code or expose it as browser actions.
Version tags label maintainer releases; they do not change the host SDK version.

Deployment supplies WorkerConfig and parent-owned, isolated Git metadata. The
publisher uses LUIGI_MAINTAINER_GITHUB_TOKEN; merge/tag/reconciliation use only
LUIGI_RELEASE_GITHUB_TOKEN. Neither identity may bypass branch protection.
Required check names are a comma-separated deployment allowlist in
LUIGI_MAINTAINER_REQUIRED_CHECKS (default: offline-regression), matching this
checkout's emitted job check rather than its workflow title.
The pending repository label must already exist.
Main must enforce strict required checks, including for admins. Humans must
mark the draft ready and satisfy any GitHub-required reviews independently of
the local release approval. No generated tests, previews or deployments run
here. The parent records verified preview readiness separately.

The parent must exclusively own candidate files and Git metadata throughout a
call. GitHub's merge API atomically compares HEAD, not the base; strict checks
are required and merge parents are verified afterward. A racing main update
therefore becomes a partial outcome, never an automatically tagged release.
HTTP JSON is rejected above 64 KiB after capture by the existing worker runner;
pagination is bounded to ten pages. No live credentials/network are test inputs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import maintainer_worker as worker
from . import review


class ReleaseError(worker.AttentionRequired):
    """The parent must mark attention, never blindly retry external mutations."""


class PublishPartialError(ReleaseError):
    def __init__(self, head_commit: str, branch: str):
        self.head_commit = head_commit
        self.branch = branch
        super().__init__("Publication outcome needs attention; do not repeat the push.")


class ReleasePartialError(ReleaseError):
    """Merge may have happened; None means its commit could not be verified."""

    def __init__(self, merge_commit: str | None, tag: str, error: str = "Release outcome needs attention."):
        self.merge_commit = merge_commit
        self.tag = tag
        self.error = "Release outcome needs attention."
        super().__init__("Release outcome needs attention; reconcile before any further mutation.")


MAX_API_BYTES = 65_536
MAX_API_PAGES = 10
MAX_PATCH_BYTES = 4_000_000


def _repository(config: worker.WorkerConfig) -> str:
    try:
        host, repository = worker._repository_identity(config.repository_url)
        parsed = urlsplit(config.repository_url)
        valid = (
            host == "github.com" and parsed.netloc == "github.com"
            and parsed.path in (f"/{repository}", f"/{repository}.git")
            and re.fullmatch(r"[A-Za-z0-9_-]{1,100}/[A-Za-z0-9_.-]{1,100}", repository)
            and repository.split("/")[1] not in {".", ".."}
            and config.base_branch == "main"
            and worker.REMOTE_RE.fullmatch(config.remote)
        )
    except (ValueError, worker.WorkerError):
        valid = False
    if not valid:
        raise ReleaseError("A fixed credential-free github.com repository on main is required.")
    return repository


def _load(
    supplied: dict[str, Any], *states: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        current = review.get_run(supplied["id"])
        if (
            current is None or current != supplied or current["state"] not in states
            or type(supplied["revision"]) is not int or current["repository_id"] != "host"
            or current["branch"] != f"automation/review-{uuid.UUID(current['id']).hex}"
        ):
            raise ValueError
        review._commit(current["base_commit"])
        review._digest(current["approved_diff_sha256"])
        review._digest(current["approved_validation_digest"])
        version = review._version(current["version"])
        if current["expected_tag"] != f"v{version}":
            raise ValueError
        option = review.get_option(current["selected_option_id"])
        if (
            option is None or option["run_id"] != current["id"]
            or option["id"] != current["selected_option_id"]
            or option["tests_passed"] is not True
            or option["diff_sha256"] != current["approved_diff_sha256"]
            or option["validation_digest"] != current["approved_validation_digest"]
        ):
            raise ValueError
        if "releasing" in states and current["state"] == "releasing":
            review._release_authorized(current)
        return current, option
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ReleaseError("Review approval or claimed revision is no longer valid.") from None


def _sha(value: Any) -> str:
    try:
        return review._commit(value)
    except (ValueError, TypeError):
        raise ReleaseError("GitHub commit identity is invalid.") from None


def _gh_env(config: worker.WorkerConfig, *, releasing: bool) -> dict[str, str]:
    _repository(config)
    name = "LUIGI_RELEASE_GITHUB_TOKEN" if releasing else "LUIGI_MAINTAINER_GITHUB_TOKEN"
    if not os.environ.get(name, "").strip():
        raise ReleaseError("The controller credential for this role is not configured.")
    if releasing:
        environment = worker._command_env()
        environment["GH_TOKEN"] = os.environ[name]
    else:
        environment = worker._gh_env(config)
    environment.update({
        "GH_HOST": "github.com", "GH_PROMPT_DISABLED": "1",
        "GH_NO_UPDATE_NOTIFIER": "1", "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1",
        "GH_CONFIG_DIR": str(config.state_root / "release-gh"),
    })
    return environment


def _api(
    config: worker.WorkerConfig, endpoint: str, *, releasing: bool = False,
    method: str = "GET", fields: dict[str, str] | None = None, missing: bool = False,
) -> dict[str, Any] | None:
    repository = _repository(config)
    command = [
        "gh", "api", "--hostname", "github.com", "--include", "--method", method,
        "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28",
        f"repos/{repository}/{endpoint}",
    ]
    for name, value in (fields or {}).items():
        command.extend(["-f", f"{name}={value}"])
    try:
        result = worker._run(
            command, cwd=config.state_root, env=_gh_env(config, releasing=releasing),
            timeout=60, check=False, label="release GitHub request",
        )
        output = result.stdout or ""
        if len(output.encode("utf-8")) > MAX_API_BYTES:
            raise ValueError
        header, separator, body = output.replace("\r\n", "\n").partition("\n\n")
        status = re.match(r"HTTP/\S+ (\d{3})(?: |$)", header.split("\n", 1)[0])
        if not separator or status is None:
            raise ValueError
        code = int(status.group(1))
        if code == 404 and missing:
            return None
        if result.returncode or not 200 <= code < 300:
            raise ValueError
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except (ValueError, TypeError, worker.WorkerError):
        raise ReleaseError("GitHub verification failed or was unavailable.") from None


def _get(config: worker.WorkerConfig, endpoint: str, *, releasing: bool) -> dict[str, Any]:
    result = _api(config, endpoint, releasing=releasing)
    if result is None:
        raise ReleaseError("GitHub verification is incomplete.")
    return result


def _pr(
    config: worker.WorkerConfig, run: dict[str, Any], *, releasing: bool, closed: bool = False,
) -> dict[str, Any]:
    repository = _repository(config)
    number = run.get("pr_number")
    if type(number) is not int or not 1 <= number <= 2_147_483_647:
        raise ReleaseError("A verified pull request number is required.")
    pull = _get(config, f"pulls/{number}", releasing=releasing)
    try:
        if (
            pull["number"] != number or type(pull["number"]) is not int
            or pull["html_url"] != f"https://github.com/{repository}/pull/{number}"
            or run["pr_url"] != pull["html_url"]
            or pull["head"]["ref"] != run["branch"]
            or _sha(pull["head"]["sha"]) != _sha(run["head_commit"])
            or pull["base"]["ref"] != "main"
            or pull["head"]["repo"]["full_name"] != repository
            or pull["base"]["repo"]["full_name"] != repository
            or (not closed and (pull["state"] != "open" or pull["merged"] is not False))
        ):
            raise ValueError
        return pull
    except (KeyError, TypeError, ValueError):
        raise ReleaseError("Pull request identity or state changed.") from None


def _required_checks() -> tuple[str, ...]:
    raw = os.environ.get("LUIGI_MAINTAINER_REQUIRED_CHECKS", "offline-regression")
    names = tuple(value.strip() for value in raw.split(","))
    if (
        len(raw) > 2000 or not 1 <= len(names) <= 20 or len(set(names)) != len(names)
        or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _./():-]{0,99}", name) for name in names)
    ):
        raise ReleaseError("Required GitHub check names must be configured explicitly.")
    return names


def _protection(
    config: worker.WorkerConfig, run: dict[str, Any], required: tuple[str, ...], *, releasing: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    branch = _get(config, "branches/main", releasing=releasing)
    protection = _get(config, "branches/main/protection", releasing=releasing)
    try:
        checks = protection["required_status_checks"]
        contexts = checks["contexts"]
        bindings = checks.get("checks", [])
        if not isinstance(contexts, list) or not all(isinstance(name, str) for name in contexts):
            raise ValueError
        if not isinstance(bindings, list) or not all(isinstance(item, dict) for item in bindings):
            raise ValueError
        protected_names = set(contexts) | {item["context"] for item in bindings}
        if (
            branch["name"] != "main" or branch["protected"] is not True
            or _sha(branch["commit"]["sha"]) != run["base_commit"]
            or checks["strict"] is not True or not set(required) <= protected_names
            or protection["enforce_admins"]["enabled"] is not True
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ReleaseError("Main moved or strict, non-bypassable required-check protection is unavailable.") from None
    return branch, protection


def _check_pages(
    config: worker.WorkerConfig, head: str, kind: str, *, releasing: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    key = "check_runs" if kind == "check-runs" else "statuses"
    records: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    total: int | None = None
    size = 0
    for page in range(1, MAX_API_PAGES + 1):
        query = f"per_page=100&page={page}"
        if kind == "check-runs":
            query += "&filter=latest"
        result = _get(config, f"commits/{head}/{kind}?{query}", releasing=releasing)
        size += len(json.dumps(result, ensure_ascii=True).encode("ascii"))
        batch = result.get(key)
        count = result.get("total_count")
        if (
            size > MAX_API_BYTES or not isinstance(batch, list) or len(batch) > 100
            or not all(isinstance(item, dict) for item in batch)
            or type(count) is not int or not 0 <= count <= 100 * MAX_API_PAGES
            or (total is not None and total != count)
            or (kind == "status" and (
                result.get("sha") != head or (count and result.get("state") != "success")
            ))
        ):
            raise ReleaseError("GitHub checks are incomplete, failed or changed during verification.")
        total = count
        evidence.append(result)
        records.extend(batch)
        if len(records) == total:
            return records, evidence
        if len(batch) != 100 or len(records) > total:
            break
    raise ReleaseError("GitHub check pagination could not be verified within limits.")


def _verify_testing(
    config: worker.WorkerConfig, run: dict[str, Any], *, releasing: bool,
) -> dict[str, Any]:
    current, _ = _load(run, "testing", "release_queued", "releasing")
    required = _required_checks()
    pull = _pr(config, current, releasing=releasing)
    if pull.get("base", {}).get("sha") != current["base_commit"]:
        raise ReleaseError("Pull request base moved; prepare and approve a newly tested candidate.")
    branch, protection = _protection(config, current, required, releasing=releasing)
    head = _sha(current["head_commit"])
    checks, check_evidence = _check_pages(config, head, "check-runs", releasing=releasing)
    statuses, status_evidence = _check_pages(config, head, "status", releasing=releasing)
    for name in required:
        matches = [item for item in checks if item.get("name") == name]
        contexts = [item for item in statuses if item.get("context") == name]
        app_ids = {
            item.get("app_id") for item in protection["required_status_checks"].get("checks", [])
            if item.get("context") == name and item.get("app_id") not in (None, -1)
        }
        if (
            not (matches or contexts)
            or any(item.get("head_sha") != head or item.get("status") != "completed"
                   or item.get("conclusion") != "success" for item in matches)
            or any(item.get("state") != "success" for item in contexts)
            or (app_ids and (contexts or any(
                not isinstance(item.get("app"), dict) or item["app"].get("id") not in app_ids
                for item in matches
            )))
        ):
            raise ReleaseError("Every configured required check must pass on the exact approved HEAD.")
    evidence = [pull, branch, protection, check_evidence, status_evidence]
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    _load(run, "testing", "release_queued", "releasing")
    return {
        "head_commit": head, "checks_passed": True, "base_commit": current["base_commit"],
        "pr_number": current["pr_number"], "checks_digest": digest,
    }


def verify_testing(config: worker.WorkerConfig, run: dict[str, Any]) -> dict[str, Any]:
    """Read live GitHub evidence; never infer preview readiness or accept CI receipts."""
    return _verify_testing(config, run, releasing=False)


def _git(config: worker.WorkerConfig, arguments: list[str], *, cwd: Path, auth: bool = False):
    try:
        return worker._git(
            config, ["-c", "core.fsmonitor=false", "-c", "core.attributesFile=/dev/null", *arguments],
            cwd=cwd, auth=auth, label="approved candidate Git operation",
        )
    except (worker.WorkerError, KeyError):
        raise ReleaseError("Approved candidate Git operation failed.") from None


def _git_metadata(config: worker.WorkerConfig, cwd: Path) -> None:
    output = _git(config, ["config", "--local", "--no-includes", "--null", "--list"], cwd=cwd).stdout
    forbidden = (
        "include.", "includeif.", "filter.", "diff.", "merge.", "credential.",
        "url.", "http.", "https.", "protocol.", "core.sshcommand", "core.gitproxy",
        "core.fsmonitor", "core.attributesfile", "core.hookspath", "core.worktree",
        "extensions.worktreeconfig", "push.", "fetch.", "submodule.",
    )
    for record in output.split("\0"):
        if not record:
            continue
        name = record.partition("\n")[0].lower()
        if name.startswith(forbidden) or re.fullmatch(r"remote\..*\.(?:vcs|proxy|receivepack|uploadpack)", name):
            raise ReleaseError("Candidate Git configuration can execute code or redirect credentials.")
    common = _git(config, ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd).stdout.strip()
    if Path(common).resolve() != (config.repository_root / ".git").absolute():
        raise ReleaseError("Candidate Git metadata is outside the isolated controller repository.")


def _remote_heads(config: worker.WorkerConfig, branch: str, cwd: Path) -> dict[str, str]:
    refs = ("refs/heads/main", f"refs/heads/{branch}")
    result = _git(
        config, ["ls-remote", "--heads", config.repository_url, *refs], cwd=cwd, auth=True,
    )
    heads = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2 or parts[1] not in refs or parts[1] in heads:
            raise ReleaseError("Remote branch identities could not be verified.")
        heads[parts[1]] = _sha(parts[0])
    return heads


def _patch_digest(config: worker.WorkerConfig, cwd: Path, *revision: str) -> str:
    with tempfile.TemporaryDirectory(prefix="luigi-approved-diff-") as directory:
        output = Path(directory) / "patch.diff"
        _git(config, [
            "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv",
            f"--output={output}", *revision, "--",
        ], cwd=cwd)
        if not output.is_file() or not 0 < output.stat().st_size <= MAX_PATCH_BYTES:
            raise ReleaseError("The candidate patch is empty or exceeds the bounded patch limit.")
        return hashlib.sha256(output.read_bytes()).hexdigest()


def _file_policy(config: worker.WorkerConfig, context: worker.WorktreeContext) -> list[str]:
    files = worker.changed_files(config, context)
    if not files:
        raise ReleaseError("The candidate has no changes.")
    for relative in files:
        parts = Path(relative).parts
        name = parts[-1].lower() if parts else ""
        if (
            name in {
                "pyproject.toml", "setup.py", "setup.cfg", ".gitattributes", ".gitmodules",
                "package.json", "package-lock.json", "pnpm-lock.yaml", "pipfile",
            }
            or name.startswith("requirements") or name.endswith(".lock")
            or relative.replace("\\", "/").lower().startswith("module-repos/feedback/")
        ):
            raise ReleaseError("Dependency and controller policy files cannot be published automatically.")
    worker.enforce_change_policy(config, context, files)
    worker.validate_static_changes(config, context, files)
    return files


def publish_candidate(
    config: worker.WorkerConfig, run: dict[str, Any], option: dict[str, Any],
    worktree: Path | worker.WorktreeContext,
) -> dict[str, Any]:
    """Publish one exact approved patch from a parent-prepared, private worktree.

    No cleanup or retries occur here. After any ambiguous push/PR outcome the
    parent must preserve the receipt/exception metadata and mark attention.
    """
    repository = _repository(config)
    current, selected = _load(run, "publishing")
    if option != selected:
        raise ReleaseError("Supplied option differs from the immutable approved option.")
    _gh_env(config, releasing=False)
    context = worktree if isinstance(worktree, worker.WorktreeContext) else worker.WorktreeContext(
        Path(worktree), current["branch"], current["base_commit"],
    )
    path = context.path.resolve()
    root = config.worktrees_root.resolve()
    if (
        not path.is_relative_to(root) or path == root or not path.is_dir()
        or context.path.absolute() != path or context.branch_name != current["branch"]
        or context.base_commit != current["base_commit"]
    ):
        raise ReleaseError("Candidate worktree does not match the claimed review.")
    _git_metadata(config, path)
    if (
        _git(config, ["rev-parse", "--show-toplevel"], cwd=path).stdout.strip().replace("\\", "/")
        != path.as_posix()
        or _git(config, ["rev-parse", "HEAD"], cwd=path).stdout.strip() != current["base_commit"]
        or _git(config, ["merge-base", "HEAD", current["base_commit"]], cwd=path).stdout.strip()
        != current["base_commit"]
        or _git(config, ["symbolic-ref", "--short", "HEAD"], cwd=path).stdout.strip() != current["branch"]
    ):
        raise ReleaseError("Candidate must have exactly the approved base and fixed review branch.")
    _file_policy(config, context)
    _git(config, ["add", "--intent-to-add", "--all", "--"], cwd=path)
    if _patch_digest(config, path, "HEAD") != current["approved_diff_sha256"]:
        raise ReleaseError("Candidate patch differs from the approved raw Git diff.")
    refs = _remote_heads(config, current["branch"], path)
    if refs != {"refs/heads/main": current["base_commit"]}:
        raise ReleaseError("Main moved or the review branch already exists; publication is blocked.")
    _load(run, "publishing")
    _file_policy(config, context)
    _git(config, ["add", "--all", "--"], cwd=path)
    if _patch_digest(config, path, "--cached", "HEAD") != current["approved_diff_sha256"]:
        raise ReleaseError("Staged patch differs from the approved patch.")
    _git(config, ["diff", "--cached", "--check", "--"], cwd=path)
    title = f"Approved feedback {current['id']} ({current['version']})"
    _git(config, [
        "-c", "user.name=Luigi Maintainer", "-c", "user.email=luigi-maintainer@example.test",
        "commit", "--no-gpg-sign", "-m", title,
    ], cwd=path)
    head = _sha(_git(config, ["rev-parse", "HEAD"], cwd=path).stdout.strip())
    parents = _git(config, ["rev-list", "--parents", "-n", "1", "HEAD"], cwd=path).stdout.split()
    if parents != [head, current["base_commit"]] or _patch_digest(
        config, path, current["base_commit"], head,
    ) != current["approved_diff_sha256"]:
        raise ReleaseError("Committed history or patch differs from the approved candidate.")
    if _remote_heads(config, current["branch"], path) != {"refs/heads/main": current["base_commit"]}:
        raise ReleaseError("Remote branches changed before publication.")
    _load(run, "publishing")
    try:
        ref = f"refs/heads/{current['branch']}"
        _git(config, [
            "push", "--porcelain", f"--force-with-lease={ref}:", config.repository_url, f"{head}:{ref}",
        ], cwd=path, auth=True)
        if _remote_heads(config, current["branch"], path) != {
            "refs/heads/main": current["base_commit"], ref: head,
        }:
            raise ReleaseError("Published branch verification failed.")
        _load(run, "publishing")
        result = worker._run([
            "gh", "pr", "create", "--draft", "--repo", f"github.com/{repository}",
            "--base", "main", "--head", current["branch"], "--title", title,
            "--label", "pending", "--body",
            "Design approved. GitHub checks and human testing are pending. No deployment is performed.",
        ], cwd=config.state_root, env=_gh_env(config, releasing=False), timeout=60,
            label="approved draft creation")
        match = re.fullmatch(
            rf"https://github\.com/{re.escape(repository)}/pull/([1-9][0-9]{{0,9}})", result.stdout.strip(),
        )
        if match is None:
            raise ReleaseError("Published pull request identity is invalid.")
        receipt = {
            "head_commit": head, "branch": current["branch"],
            "pr_url": match.group(0), "pr_number": int(match.group(1)),
        }
        pull = _pr(config, {**current, **receipt}, releasing=False)
        if pull.get("draft") is not True or pull.get("base", {}).get("sha") != current["base_commit"]:
            raise ReleaseError("Published pull request is not the approved draft.")
        _load(run, "publishing")
        return receipt
    except (worker.WorkerError, ValueError, TypeError, KeyError):
        raise PublishPartialError(head, current["branch"]) from None


def _tag(config: worker.WorkerConfig, tag: str) -> dict[str, Any] | None:
    return _api(config, f"git/ref/tags/{tag}", releasing=True, missing=True)


def _tag_matches(record: dict[str, Any] | None, tag: str, commit: str) -> bool:
    return bool(
        record and record.get("ref") == f"refs/tags/{tag}"
        and isinstance(record.get("object"), dict)
        and record["object"].get("type") == "commit" and record["object"].get("sha") == commit
    )


def _github_reviews(
    config: worker.WorkerConfig, run: dict[str, Any], protection: dict[str, Any],
) -> None:
    policy = protection.get("required_pull_request_reviews")
    if policy is None:
        return
    if not isinstance(policy, dict):
        raise ReleaseError("GitHub review protection is incomplete.")
    count = policy.get("required_approving_review_count")
    if type(count) is not int or not 0 <= count <= 6:
        raise ReleaseError("GitHub review protection is incomplete.")
    if not count and policy.get("require_code_owner_reviews") is not True:
        return
    try:
        result = worker._run([
            "gh", "pr", "view", str(run["pr_number"]), "--repo", f"github.com/{_repository(config)}",
            "--json", "reviewDecision,headRefOid,headRefName,baseRefName,number,url",
        ], cwd=config.state_root, env=_gh_env(config, releasing=True), timeout=60,
            label="GitHub review verification")
        if len(result.stdout.encode("utf-8")) > MAX_API_BYTES:
            raise ValueError
        decision = json.loads(result.stdout)
        if (
            decision["reviewDecision"] != "APPROVED" or decision["headRefOid"] != run["head_commit"]
            or decision["headRefName"] != run["branch"] or decision["baseRefName"] != "main"
            or decision["number"] != run["pr_number"] or decision["url"] != run["pr_url"]
        ):
            raise ValueError
    except (worker.WorkerError, ValueError, KeyError, TypeError):
        raise ReleaseError("GitHub-required human reviews must be approved before release.") from None


def _merged_commit(config: worker.WorkerConfig, run: dict[str, Any], merge: str) -> None:
    commit = _get(config, f"git/commits/{merge}", releasing=True)
    parents = commit.get("parents")
    if (
        commit.get("sha") != merge or not isinstance(parents, list)
        or len(parents) != 2 or not all(isinstance(parent, dict) for parent in parents)
        or [parent.get("sha") for parent in parents] != [run["base_commit"], run["head_commit"]]
    ):
        raise ReleaseError("Merge commit parents do not match the approved base and tested HEAD.")


def merge_release(config: worker.WorkerConfig, run: dict[str, Any]) -> dict[str, str]:
    """Merge the approved tested HEAD with normal protection, then create its tag.

    On ReleasePartialError, persist merge_commit/tag and mark attention. Never
    automatically retry a merge or tag, even after a timeout. Reconciliation is
    read-only. No GitHub release object, package bump, preview or restart occurs.
    """
    _repository(config)
    current, _ = _load(run, "releasing")
    from .test_preview import verify_ready

    try:
        verify_ready(current)
    except (ValueError, OSError):
        raise ReleaseError("The tested preview is no longer ready for this commit.") from None
    _verify_testing(config, current, releasing=True)
    tag = current["expected_tag"]
    if _tag(config, tag) is not None:
        raise ReleaseError("The literal approved version tag already exists.")
    pull = _pr(config, current, releasing=True)
    if (
        pull.get("mergeable") is not True or pull.get("draft") is not False
        or pull.get("mergeable_state") != "clean" or pull.get("base", {}).get("sha") != current["base_commit"]
    ):
        raise ReleaseError("GitHub must report a ready, clean, mergeable pull request.")
    _, protection = _protection(config, current, _required_checks(), releasing=True)
    _github_reviews(config, current, protection)
    _load(run, "releasing")
    try:
        verify_ready(current)
    except (ValueError, OSError):
        raise ReleaseError("The tested preview changed before merge.") from None
    merge: str | None = None
    try:
        result = _api(config, f"pulls/{current['pr_number']}/merge", releasing=True, method="PUT", fields={
            "sha": current["head_commit"], "merge_method": "merge",
        })
        if result is None or result.get("merged") is not True:
            raise ReleaseError("GitHub did not confirm the requested merge.")
        merge = _sha(result.get("sha"))
        merged = _pr(config, current, releasing=True, closed=True)
        if merged.get("merged") is not True or merged.get("state") != "closed" or merged.get("merge_commit_sha") != merge:
            raise ReleaseError("GitHub did not verify the merged pull request.")
        _merged_commit(config, current, merge)
        branch = _get(config, "branches/main", releasing=True)
        if branch.get("protected") is not True or branch.get("commit", {}).get("sha") != merge:
            raise ReleaseError("Main changed before the release tag could be verified.")
        _load(run, "releasing")
        if _tag(config, tag) is not None:
            raise ReleaseError("The approved tag was claimed during the merge.")
        created = _api(config, "git/refs", releasing=True, method="POST", fields={
            "ref": f"refs/tags/{tag}", "sha": merge,
        })
        if not _tag_matches(created, tag, merge) or not _tag_matches(_tag(config, tag), tag, merge):
            raise ReleaseError("The release tag could not be verified.")
        _load(run, "releasing")
        return {"merge_commit": merge, "tag": tag}
    except (worker.WorkerError, ValueError, TypeError, KeyError):
        raise ReleasePartialError(merge, tag) from None


def reconcile_release(config: worker.WorkerConfig, run: dict[str, Any]) -> dict[str, Any]:
    """Read identities after uncertainty; never merge, tag, approve or change state."""
    _repository(config)
    current, _ = _load(run, "releasing", "needs_attention", "released")
    pull = _pr(config, current, releasing=True, closed=True)
    tag = current["expected_tag"]
    remote_tag = _tag(config, tag)
    merge: str | None = None
    if pull.get("merged") is True:
        if pull.get("state") != "closed":
            raise ReleaseError("Merged pull request state is inconsistent.")
        merge = _sha(pull.get("merge_commit_sha"))
        _merged_commit(config, current, merge)
        status = "released" if _tag_matches(remote_tag, tag, merge) else (
            "merged_untagged" if remote_tag is None else "tag_conflict"
        )
    elif pull.get("merged") is False and pull.get("state") in {"open", "closed"}:
        status = "tag_conflict" if remote_tag is not None else "not_merged"
    else:
        raise ReleaseError("Pull request outcome could not be reconciled.")
    _load(run, "releasing", "needs_attention", "released")
    return {
        "status": status, "head_commit": current["head_commit"],
        "merge_commit": merge, "tag": tag, "pr_number": current["pr_number"],
    }
