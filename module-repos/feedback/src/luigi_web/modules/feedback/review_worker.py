"""Approval-gated review controllers; candidate code runs only in sandbox.py.

The parent CLI must dispatch roles separately, never call the legacy draft-PR
worker by default. The web process may read artifact_path PNGs, not private
state_root/reviews patches. Deployment supplies a shared queue group and an
artifact directory outside all live checkouts and application data directories.

Controller contracts (mapping or dataclass receipts):
publish_candidate(config, run, option, worktree_path) -> head_commit, branch,
pr_url, pr_number; verify_testing(config, run) -> head_commit, checks_passed;
merge_release(config, run) -> merge_commit, tag. These functions own all remote
verification and mutations. Missing controllers fail before queue claims.
The parent must provision an isolated live-preview controller for preview_once;
generation screenshots are not interactive preview attestations.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
from typing import Any, Callable
from urllib.parse import urlsplit
import uuid

from . import maintainer, review, sandbox
from . import maintainer_worker as legacy

WorkerConfig = legacy.WorkerConfig
MAX_PATCH_BYTES = 1024 * 1024
IMAGE_SIZES = {"desktop.png": (1440, 900), "mobile.png": (390, 844)}
VARIANTS = (
    "Prefer the smallest conventional change, preserving the existing layout and interactions.",
    "Prefer a compact, scan-friendly arrangement within the existing design system.",
    "Prefer clear grouping and progressive disclosure within the existing design system.",
)


@dataclass(frozen=True)
class WorkerResult:
    status: str
    job_uuid: str = ""
    run_id: str = ""
    notification_sent: bool = False


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _identifier(value: str) -> str:
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid artifact identifier.") from None
    return value


def _plain_path(path: Path) -> Path:
    if (not path.is_absolute() or ".." in path.parts or any(char in str(path) for char in "!\x00\r\n")
            or str(path).startswith(("\\\\", "//"))):
        raise ValueError("Invalid artifact directory.")
    for parent in (*reversed(path.parents), path):
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise ValueError("Linked artifact paths are forbidden.")
    return path


def _protected_root(path: Path) -> Path:
    path = _plain_path(path)
    if path == path.parent:
        raise ValueError("An explicit artifact directory is required.")
    forbidden = [Path(value).absolute() for key in (
        "LUIGI_WEB_DATA_DIR", "LUIGI_WEB_REPOSITORY_ROOT",
    ) if (value := os.environ.get(key))]
    forbidden.extend(parent for parent in Path(__file__).resolve().parents
                     if (parent / "luigi_web" / "application.py").is_file())
    for key in ("LUIGI_WEB_FINANCE_DB", "LUIGI_WEB_CARDS_DB", "LUIGI_WEB_RPG_DB", "LUIGI_WEB_FEEDBACK_DB"):
        if value := os.environ.get(key):
            forbidden.append(Path(value).absolute().parent)
    if any(path == root or root in path.parents for root in forbidden):
        raise ValueError("Artifacts must be outside live source and application data.")
    return path


def _artifact_root() -> Path:
    queue = Path(os.environ.get("LUIGI_MAINTAINER_QUEUE_DIR", "/var/lib/luigi-maintainer-queue"))
    return _protected_root(Path(os.environ.get("LUIGI_MAINTAINER_ARTIFACT_DIR", str(queue / "review-artifacts"))))


def artifact_path(run_id: str, artifact_id: str, name: str) -> Path:
    """Authenticated artifact loader: opaque IDs plus a fixed reviewed basename.

    Resolve the screenshot ID from review.candidate_metadata, check its SHA256,
    and serve no-store. Only the screened patch and synthetic PNGs are shared.
    This function does not create directories or follow symbolic links.
    """
    if name not in {*IMAGE_SIZES, "candidate.patch"}:
        raise ValueError("Unknown review image.")
    return _plain_path(_artifact_root() / _identifier(run_id) / _identifier(artifact_id) / name)


def _directory(path: Path, *, shared: bool = False) -> Path:
    _plain_path(path)
    path.mkdir(mode=0o750 if shared else 0o700, parents=True, exist_ok=True)
    _plain_path(path)
    if not path.is_dir():
        raise ValueError("Artifact directory is unavailable.")
    if os.name == "posix":
        path.chmod(0o750 if shared else 0o700)
        if shared:
            queue = _plain_path(Path(os.environ.get("LUIGI_MAINTAINER_QUEUE_DIR", "/var/lib/luigi-maintainer-queue")))
            os.chown(path, -1, queue.stat().st_gid)
    return path


def _read(path: Path, limit: int) -> bytes:
    _plain_path(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError("Artifact is not a bounded regular file.")
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Artifact exceeds its byte limit.")
    return content


def _write(path: Path, content: bytes, *, shared: bool = False) -> None:
    _plain_path(path)
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name == "posix":
        path.chmod(0o640 if shared else 0o400)
        if shared:
            os.chown(path, -1, path.parent.stat().st_gid)


def _private_root(config: WorkerConfig, run_id: str) -> Path:
    return _plain_path(_protected_root(config.state_root) / "reviews" / _identifier(run_id))


def _check_roots(config: WorkerConfig) -> None:
    private = _protected_root(config.state_root)
    public = _artifact_root()
    if private == public or private in public.parents or public in private.parents:
        raise ValueError("Public images and private worker state must be separate.")


def _source_files(context: legacy.WorktreeContext, files: list[str]) -> None:
    if not files or len(files) > legacy.MAX_CHANGED_FILES or len(set(files)) != len(files):
        raise ValueError("Invalid candidate file set.")
    workspace = legacy.SafeWorkspace(context.path)
    for relative in files:
        path = workspace.assert_writable_path(relative)
        if path.exists():
            content = _read(path, MAX_PATCH_BYTES)
            content.decode("utf-8", errors="strict")
            if b"\0" in content:
                raise ValueError("Binary source changes are forbidden.")


def _patch_bytes(content: bytes) -> bytes:
    if not isinstance(content, bytes) or not content or len(content) > MAX_PATCH_BYTES:
        raise ValueError("Invalid candidate patch size.")
    content.decode("utf-8", errors="strict")
    if b"\0" in content or b"GIT binary patch" in content or b"\nBinary files " in content:
        raise ValueError("Binary patches are forbidden.")
    if not content.startswith(b"diff --git "):
        raise ValueError("Invalid candidate patch.")
    return content


def capture_patch(config: WorkerConfig, context: legacy.WorktreeContext, files: list[str]) -> bytes:
    """Controller-owned Git only; preserve exact bytes, including new text files."""
    _source_files(context, files)
    legacy._git(config, ["add", "--intent-to-add", "--", *files], cwd=context.path, label="candidate intent staging")
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "credential.helper=", "-c", "diff.external=",
         "diff", "--no-ext-diff", "--no-textconv", "--binary", "--full-index", "HEAD", "--"],
        cwd=context.path, env=legacy._command_env(), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60, check=False,
    )
    if result.returncode:
        raise ValueError("Candidate patch capture failed.")
    return _patch_bytes(result.stdout)


def _test_results(evidence: sandbox.TestEvidence, digest: str) -> list[dict[str, Any]]:
    checks = {check.name: check for check in evidence.checks}
    if len(checks) != len(evidence.checks) or set(checks) - set(sandbox.CHECK_NAMES):
        raise ValueError("Invalid sandbox checks.")
    for check in checks.values():
        if type(check.passed) is not bool or type(check.exit_code) is not int:
            raise ValueError("Invalid sandbox check result.")

    def result(command: str, names: tuple[str, ...], total: int) -> dict[str, Any]:
        passed = bool(evidence.passed is True and not evidence.reason and type(evidence.exit_code) is int
                  and evidence.exit_code == 0 and total > 0 and all(
            name in checks and checks[name].passed is True and checks[name].exit_code == 0 for name in names
        ))
        return {"command_id": command, "passed": passed, "total": total,
                "failed": 0 if passed or total == 0 else 1, "skipped": 0, "output_digest": digest}

    def count(name: str) -> int:
        value = checks[name].count if name in checks else None
        if value is None:
            return 0
        if type(value) is not int or not 0 <= value <= 1_000_000:
            raise ValueError("Invalid sandbox count.")
        return value

    tests = result("unittest", ("tests",), count("tests"))
    tests["passed"] = tests["passed"] and type(evidence.tests_run) is int and evidence.tests_run == tests["total"]
    validator = result("validator", ("templates", "routes"), min(1_000_000, count("templates") + count("routes")))
    validator["passed"] = validator["passed"] and count("templates") > 0 and count("routes") > 0
    design = result("design_review", ("screenshots",), len(evidence.screenshots))
    design["passed"] = design["passed"] and count("screenshots") == 2 and {
        (image.name, image.width, image.height) for image in evidence.screenshots
    } == {(name, *size) for name, size in IMAGE_SIZES.items()} and len(evidence.screenshots) == 2
    results = [tests, validator, design]
    if "whitespace" in checks:
        whitespace = result("diff_check", ("whitespace",), 1)
        whitespace["passed"] = whitespace["passed"] and count("whitespace") == 0
        results.append(whitespace)
    return sorted(results, key=lambda item: item["command_id"])


def _validation_digest(metadata: dict[str, Any]) -> str:
    return _sha(_json({field: metadata[field] for field in (
        "diff_sha256", "tree_sha256", "artifact_id", "test_results", "screenshots",
    )}))


def _save_candidate(
    config: WorkerConfig, run: dict[str, Any], artifact_id: str, patch: bytes,
    files: list[str], evidence: sandbox.TestEvidence,
) -> dict[str, Any]:
    root = _private_root(config, run["id"]) / _identifier(artifact_id)
    _plain_path(root)
    if not root.is_dir() or evidence.candidate_id != artifact_id:
        raise ValueError("Candidate evidence is missing.")
    if not re.fullmatch(r"[0-9a-f]{64}", evidence.source_digest) or not re.fullmatch(sandbox.IMAGE_PATTERN, evidence.image):
        raise ValueError("Candidate source or sandbox image digest is missing.")
    evidence_bytes = _read(root / "evidence.json", sandbox.MAX_REPORT_BYTES)
    if json.loads(evidence_bytes) != json.loads(_json(asdict(evidence))):
        raise ValueError("Sandbox evidence changed.")
    images = []
    image_names = {}
    for image in evidence.screenshots:
        if image.name not in IMAGE_SIZES or (image.width, image.height) != IMAGE_SIZES[image.name]:
            raise ValueError("Unexpected screenshot.")
        content = _read(root / image.name, sandbox.MAX_PNG_BYTES)
        sandbox._png_info(content, IMAGE_SIZES[image.name])
        if _sha(content) != image.sha256:
            raise ValueError("Screenshot digest mismatch.")
        image_id = str(uuid.uuid4())
        target = artifact_path(run["id"], image_id, image.name)
        _directory(_artifact_root(), shared=True)
        _directory(target.parent.parent, shared=True)
        _directory(target.parent, shared=True)
        _write(target, content, shared=True)
        images.append({"artifact_id": image_id, "sha256": image.sha256, "width": image.width, "height": image.height})
        image_names[image_id] = image.name
    metadata = {
        "base_commit": run["base_commit"], "artifact_id": artifact_id,
        "diff_sha256": _sha(patch), "tree_sha256": evidence.source_digest,
        "image": evidence.image, "evidence_sha256": _sha(evidence_bytes),
        "test_results": _test_results(evidence, _sha(evidence_bytes)),
        "screenshots": sorted(images, key=lambda item: item["artifact_id"]),
        "image_names": image_names, "files": files,
    }
    metadata["validation_digest"] = _validation_digest(metadata)
    _write(root / "source.patch", _patch_bytes(patch))
    _write(root / "metadata.json", _json(metadata))
    public_patch = artifact_path(run["id"], artifact_id, "candidate.patch")
    _directory(_artifact_root(), shared=True)
    _directory(public_patch.parent.parent, shared=True)
    _directory(public_patch.parent, shared=True)
    _write(public_patch, _patch_bytes(patch), shared=True)
    if os.name == "posix":
        for name in ("evidence.json", *[image.name for image in evidence.screenshots]):
            (root / name).chmod(0o400)
    return metadata


def _attention(run_id: str, message: str) -> None:
    current = review.get_run(run_id)
    if current and current["state"] not in {"released", "failed", "cancelled"}:
        review.mark_attention(run_id, message)


def generation_once(
    config: WorkerConfig, *, availability: Callable[..., Any] = sandbox.available,
    agent_runner: Callable[..., Any] = legacy.run_agent,
    preparer: Callable[..., Any] = legacy.prepare_worktree,
    change_reader: Callable[..., Any] = legacy.changed_files,
    policy_checker: Callable[..., Any] = legacy.enforce_change_policy,
    validator: Callable[..., Any] = legacy.validate_static_changes,
    patch_reader: Callable[..., bytes] = capture_patch,
    sandbox_runner: Callable[..., sandbox.TestEvidence] = sandbox.run_checks,
    cleaner: Callable[..., Any] = legacy.cleanup_worktree,
    monotonic: Callable[[], float] = time.monotonic,
) -> WorkerResult:
    """Generate at most three options; never publish, notify, or run host tests."""
    try:
        if availability().available is not True or config.agent_timeout < 180:
            return WorkerResult("Unavailable")
        _check_roots(config)
    except Exception:
        return WorkerResult("Unavailable")
    job = maintainer.claim_next_job()
    if job is None:
        return WorkerResult("Idle")
    context = None
    run = None
    status = "needs_attention"
    deadline = monotonic() + config.agent_timeout
    try:
        preview_kind = sandbox.preview_kind_for_path(job.get("page_path") or "/")
        context = preparer(config, dict(job))
        maintainer.set_run_context(job["uuid"], branch_name=context.branch_name, base_commit=context.base_commit)
        run = review.create_run(job["uuid"], context.base_commit, options_requested=3)
        artifact_root = _directory(_private_root(config, run["id"]))
        for index, variant in enumerate(VARIANTS):
            option_job = {key: job[key] for key in (
                "uuid", "feedback_uuid", "category", "request_text", "page_path", "acceptance_criteria", "policy_version",
            )}
            option_job["uuid"] = str(uuid.uuid4())
            option_job["request_text"] += (
                "\n\nTrusted option instruction: Keep the approved request and acceptance criteria unchanged in intent. "
                "Do not add scope or make cosmetic-only changes to force distinct patches. " + variant
            )
            try:
                if context is None:
                    context = preparer(config, option_job)
                if context.base_commit != run["base_commit"]:
                    raise legacy.AttentionRequired("Base changed during generation.")
                remaining = int(deadline - monotonic()) - sandbox.TIMEOUT_SECONDS - 60
                if remaining < 60:
                    raise legacy.AttentionRequired("Generation time budget exhausted.")
                outcome = agent_runner(context.path, option_job,
                                       copilot_home=config.copilot_root / option_job["uuid"],
                                       timeout=min(config.agent_timeout // 3, remaining))
                files = change_reader(config, context)
                if outcome.result != "ready" or not files:
                    continue
                _source_files(context, files)
                policy_checker(config, context, files)
                patch = _patch_bytes(patch_reader(config, context, files))
                validator(config, context, files)
                if int(deadline - monotonic()) < sandbox.TIMEOUT_SECONDS + 60:
                    raise legacy.AttentionRequired("Generation time budget exhausted.")
                artifact_id = str(uuid.uuid4())
                evidence = sandbox_runner(context.path, artifact_root, artifact_id, preview_kind=preview_kind)
                if patch_reader(config, context, files) != patch:
                    raise legacy.AttentionRequired("Candidate changed during validation.")
                metadata = _save_candidate(config, run, artifact_id, patch, files, evidence)
                review.add_option(
                    run["id"], label=f"Option {index + 1}",
                    summary=maintainer.sanitize_output(outcome.summary) or "Candidate prepared for review.",
                    diff_sha256=metadata["diff_sha256"], artifact_id=artifact_id,
                    tree_sha256=metadata["tree_sha256"], test_results=metadata["test_results"],
                    screenshots=metadata["screenshots"],
                    notes="Local sandbox evidence is advisory; independent CI and explicit admin approval are required.",
                )
            except legacy.AttentionRequired:
                raise
            except Exception:
                pass
            finally:
                if context is not None:
                    disposable, context = context, None
                    cleaner(config, disposable)
        status = review.finish_generation(run["id"])["state"]
    except Exception:
        if run:
            _attention(run["id"], "Generation could not complete safely; review the candidate evidence.")
    finally:
        if context is not None:
            try:
                cleaner(config, context)
            except Exception:
                if run:
                    _attention(run["id"], "Disposable worktree cleanup requires attention.")
                status = "needs_attention"
        maintainer.finish_job(job["uuid"], status="Needs attention", summary=(
            "Design options are awaiting review" if status == "awaiting_approval"
            else "Design option generation requires attention."
        ))
    return WorkerResult(status, job["uuid"], run["id"] if run else "")


@dataclass(frozen=True)
class Candidate:
    patch_path: Path
    patch: bytes
    metadata: dict[str, Any]


def load_candidate(config: WorkerConfig, run: dict[str, Any], option: dict[str, Any]) -> Candidate:
    """Read a private candidate only after binding patch, checks and both PNGs."""
    _check_roots(config)
    if option["run_id"] != run["id"]:
        raise ValueError("Candidate belongs to another review.")
    root = _private_root(config, run["id"]) / _identifier(option["artifact_id"])
    metadata = json.loads(_read(root / "metadata.json", 64 * 1024))
    patch_path = root / "source.patch"
    content = _patch_bytes(_read(patch_path, MAX_PATCH_BYTES))
    if metadata["base_commit"] != run["base_commit"] or _sha(content) != option["diff_sha256"]:
        raise ValueError("Candidate base or patch changed.")
    if _read(artifact_path(run["id"], option["artifact_id"], "candidate.patch"), MAX_PATCH_BYTES) != content:
        raise ValueError("Reviewed patch differs from the private candidate.")
    for field in ("diff_sha256", "tree_sha256", "artifact_id", "test_results", "screenshots", "validation_digest"):
        if metadata[field] != option[field]:
            raise ValueError("Candidate metadata changed.")
    if _validation_digest(metadata) != option["validation_digest"]:
        raise ValueError("Candidate validation digest changed.")
    evidence_bytes = _read(root / "evidence.json", sandbox.MAX_REPORT_BYTES)
    evidence_dict = json.loads(evidence_bytes)
    checks = tuple(sandbox.CheckEvidence(**item) for item in evidence_dict.pop("checks"))
    screenshots = tuple(sandbox.ScreenshotEvidence(**item) for item in evidence_dict.pop("screenshots"))
    evidence = sandbox.TestEvidence(**evidence_dict, checks=checks, screenshots=screenshots)
    if (evidence.candidate_id != option["artifact_id"] or evidence.source_digest != option["tree_sha256"]
            or evidence.image != metadata["image"] or _sha(evidence_bytes) != metadata["evidence_sha256"]
            or _test_results(evidence, _sha(evidence_bytes)) != option["test_results"]):
        raise ValueError("Candidate evidence changed.")
    if not re.fullmatch(sandbox.IMAGE_PATTERN, evidence.image):
        raise ValueError("Candidate image is not pinned.")
    if option["tests_passed"] is not True or not all(result["passed"] is True for result in option["test_results"]):
        raise ValueError("Candidate has not passed required checks.")
    if len(option["screenshots"]) != 2 or len(metadata["image_names"]) != 2:
        raise ValueError("Two screenshots are required.")
    by_name = {image.name: image for image in evidence.screenshots}
    seen = set()
    for image in option["screenshots"]:
        name = metadata["image_names"][image["artifact_id"]]
        if name not in IMAGE_SIZES or name in seen or (image["width"], image["height"]) != IMAGE_SIZES[name]:
            raise ValueError("Screenshot binding changed.")
        seen.add(name)
        if by_name[name].sha256 != image["sha256"]:
            raise ValueError("Screenshot evidence changed.")
        for path in (root / name, artifact_path(run["id"], image["artifact_id"], name)):
            image_bytes = _read(path, sandbox.MAX_PNG_BYTES)
            if _sha(image_bytes) != image["sha256"]:
                raise ValueError("Screenshot bytes changed.")
            sandbox._png_info(image_bytes, IMAGE_SIZES[name])
    return Candidate(patch_path, content, metadata)


def _selected(run: dict[str, Any]) -> dict[str, Any]:
    option = review.get_option(run["selected_option_id"])
    if (option is None or option["run_id"] != run["id"] or option["tests_passed"] is not True
            or option["diff_sha256"] != run["approved_diff_sha256"]
            or option["validation_digest"] != run["approved_validation_digest"]):
        raise ValueError("Approved candidate binding is invalid.")
    return option


def _controller(name: str, injected: Callable[..., Any] | None) -> Callable[..., Any] | None:
    if injected is not None:
        return injected
    from . import release
    token = "LUIGI_RELEASE_GITHUB_TOKEN" if name == "merge_release" else "LUIGI_MAINTAINER_GITHUB_TOKEN"
    if not os.environ.get(token, "").strip():
        return None
    if name == "merge_release" and os.environ.get("LUIGI_MAINTAINER_COPILOT_TOKEN", "").strip():
        return None
    controller = getattr(release, name, None)
    return controller if callable(controller) else None


def _receipt(value: Any) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if not isinstance(value, dict):
        raise ValueError("Invalid controller receipt.")
    return value


def apply_candidate(config: WorkerConfig, context: legacy.WorktreeContext, candidate: Candidate) -> None:
    if _read(candidate.patch_path, MAX_PATCH_BYTES) != candidate.patch:
        raise ValueError("Candidate patch changed before application.")
    for arguments in (["apply", "--check", "--whitespace=error-all"], ["apply", "--whitespace=error-all"]):
        legacy._git(config, [*arguments, "--", str(candidate.patch_path)], cwd=context.path, label="approved candidate application")


def select_review_branch(config: WorkerConfig, context: legacy.WorktreeContext, run: dict[str, Any]) -> legacy.WorktreeContext:
    expected = "automation/review-" + uuid.UUID(_identifier(run["id"])).hex
    if run["branch"] != expected or context.base_commit != run["base_commit"]:
        raise ValueError("Invalid review branch binding.")
    legacy._validate_branch(context.branch_name)
    legacy._git(config, ["branch", "-m", context.branch_name, expected], cwd=context.path, label="fixed review branch selection")
    return legacy.WorktreeContext(context.path, expected, context.base_commit)


def _partial_receipt(config: WorkerConfig, run: dict[str, Any], error: Exception, phase: str) -> None:
    """Preserve only controller identities for manual reconciliation, never errors."""
    receipt = {"run_id": run["id"], "phase": phase}
    if phase == "publication":
        head = getattr(error, "head_commit", None)
        if isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head) and getattr(error, "branch", None) == run["branch"]:
            receipt.update(head_commit=head, branch=run["branch"])
    else:
        merge = getattr(error, "merge_commit", None)
        if getattr(error, "tag", None) == run["expected_tag"] and (
            merge is None or isinstance(merge, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", merge)
        ):
            receipt.update(merge_commit=merge, tag=run["expected_tag"])
    if len(receipt) > 2:
        _write(_private_root(config, run["id"]) / (phase + "-receipt.json"), _json(receipt))


def publish_once(
    config: WorkerConfig, *, publisher: Callable[..., Any] | None = None,
    preparer: Callable[..., Any] = legacy.prepare_worktree,
    branch_selector: Callable[..., Any] = select_review_branch,
    patch_applier: Callable[..., Any] = apply_candidate,
    change_reader: Callable[..., Any] = legacy.changed_files,
    policy_checker: Callable[..., Any] = legacy.enforce_change_policy,
    validator: Callable[..., Any] = legacy.validate_static_changes,
    patch_reader: Callable[..., bytes] = capture_patch,
    exporter: Callable[..., Any] = sandbox.export_candidate,
    cleaner: Callable[..., Any] = legacy.cleanup_worktree,
) -> WorkerResult:
    """Publish only a claimed admin selection; no host tests or preview receipts."""
    controller = _controller("publish_candidate", publisher)
    if controller is None:
        return WorkerResult("Unavailable")
    try:
        _check_roots(config)
    except ValueError:
        return WorkerResult("Unavailable")
    run = review.claim_publish()
    if run is None:
        return WorkerResult("Idle")
    context = None
    status = "needs_attention"
    try:
        option = _selected(run)
        candidate = load_candidate(config, run, option)
        job = maintainer.get_job(run["job_uuid"])
        if job is None:
            raise ValueError("Approved job is unavailable.")
        context = preparer(config, {**job, "uuid": str(uuid.uuid4())})
        if context.base_commit != run["base_commit"]:
            raise ValueError("Base changed since option generation.")
        context = branch_selector(config, context, run)
        patch_applier(config, context, candidate)
        files = change_reader(config, context)
        _source_files(context, files)
        if sorted(files) != sorted(candidate.metadata["files"]):
            raise ValueError("Candidate file set changed.")
        policy_checker(config, context, files)
        validator(config, context, files)
        if patch_reader(config, context, files) != candidate.patch:
            raise ValueError("Applied candidate differs from the approved patch.")
        with tempfile.TemporaryDirectory(prefix="review-verify-", dir=config.state_root) as temporary:
            snapshot = exporter(context.path, Path(temporary) / "source")
        if snapshot.source_digest != option["tree_sha256"]:
            raise ValueError("Applied candidate source digest changed.")
        load_candidate(config, run, option)
        current = review.get_run(run["id"])
        if current != run:
            raise ValueError("Publication claim changed.")
        receipt = _receipt(controller(config, run, option, context.path))
        result = review.record_published(run["id"], receipt["head_commit"], receipt["branch"], receipt["pr_url"], receipt["pr_number"])
        status = result["state"]
    except Exception as error:
        try:
            _partial_receipt(config, run, error, "publication")
        except Exception:
            pass
        _attention(run["id"], "Publication could not be verified; check the external outcome before retrying.")
    finally:
        if context is not None:
            try:
                cleaner(config, context)
            except Exception:
                _attention(run["id"], "Publication worktree cleanup requires attention.")
                status = "needs_attention"
    return WorkerResult(status, run["job_uuid"], run["id"])


def refresh_once(config: WorkerConfig, *, verifier: Callable[..., Any] | None = None) -> WorkerResult:
    """Refresh at most 100 testing reviews; never authorize or perform release."""
    controller = _controller("verify_testing", verifier)
    if controller is None:
        return WorkerResult("Unavailable")
    runs = review.list_runs(state="testing", limit=100)
    if not runs:
        return WorkerResult("Idle")
    attention = False
    for run in runs:
        try:
            receipt = _receipt(controller(config, run))
            head = receipt["head_commit"]
            passed = receipt["checks_passed"]
            if type(passed) is not bool:
                raise ValueError("Invalid check receipt.")
            if head != run["head_commit"]:
                review.record_head(run["id"], head)
                _attention(run["id"], "Published HEAD differs from the approved candidate; new review is required.")
                attention = True
                continue
            review.record_test_result(run["id"], head, passed)
        except Exception:
            try:
                review.record_test_result(run["id"], run["head_commit"], False)
            except ValueError:
                attention = True
    return WorkerResult("needs_attention" if attention else "testing")


def preview_once(
    config: WorkerConfig, run_id: str, *, preview_runner: Callable[..., Any] | None = None,
) -> WorkerResult:
    """Parent live-preview hook, not a screenshot substitute.

    The protected controller must isolate candidate code from credentials/data
    and return {ready: True, head_commit: exact_SHA, preview_url: fixed_path}
    only after the published source is serving at that path. No default live
    service is available here; absent injection leaves preview_commit unset.
    """
    if preview_runner is None:
        return WorkerResult("Unavailable", run_id=_identifier(run_id))
    run = review.get_run(run_id)
    if run is None or run["state"] != "testing":
        return WorkerResult("Idle", run_id=_identifier(run_id))
    try:
        option = _selected(run)
        load_candidate(config, run, option)
        receipt = _receipt(preview_runner(config, run, option))
        if receipt.get("ready") is not True:
            return WorkerResult("Unavailable", run["job_uuid"], run_id)
        review.record_preview(run_id, receipt["head_commit"], receipt["preview_url"])
    except Exception:
        _attention(run_id, "Live preview readiness could not be verified.")
        return WorkerResult("needs_attention", run["job_uuid"], run_id)
    return WorkerResult("testing", run["job_uuid"], run_id)


def release_once(config: WorkerConfig, *, releaser: Callable[..., Any] | None = None) -> WorkerResult:
    """Separate protected role; execute only an exact admin-authorized release."""
    controller = _controller("merge_release", releaser)
    if controller is None:
        return WorkerResult("Unavailable")
    queued = review.list_runs(state="release_queued")
    if queued:
        from .test_preview import verify_ready

        try:
            verify_ready(queued[0])
        except (ValueError, OSError):
            _attention(queued[0]["id"], "The approved preview is no longer ready; release was not attempted.")
            return WorkerResult("needs_attention", run_id=queued[0]["id"])
    run = review.claim_release()
    if run is None:
        return WorkerResult("Idle")
    try:
        load_candidate(config, run, _selected(run))
        receipt = _receipt(controller(config, run))
        result = review.record_released(run["id"], receipt["merge_commit"], receipt["tag"])
        return WorkerResult(result["state"], run["job_uuid"], run["id"])
    except Exception as error:
        try:
            _partial_receipt(config, run, error, "release")
        except Exception:
            pass
        _attention(run["id"], "Release could not be verified; check merge and tag outcomes before retrying.")
        return WorkerResult("needs_attention", run["job_uuid"], run["id"])


def _review_link(run_id: str) -> str:
    value = os.environ.get("LUIGI_MAINTAINER_UI_URL", "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or any(char.isspace() or ord(char) < 32 for char in value)):
        raise ValueError("Review URL must be a credential-free origin.")
    return value.rstrip("/") + f"/feedback/reviews/{_identifier(run_id)}"


def notify_once(*, notifier: Callable[..., bool] = legacy.send_notification) -> WorkerResult:
    """Deliver one leased outbox item; disclose only opaque IDs and fixed copy."""
    try:
        _review_link(str(uuid.UUID(int=0)))
    except ValueError:
        return WorkerResult("Unavailable")
    notification = review.claim_notification()
    if notification is None:
        return WorkerResult("Idle")
    run_id = notification["run_id"]
    messages = {
        "awaiting_approval": "Options ready. Open Maintenance review in Feedback.",
        "testing": "Selected option published. Open Maintenance review in Feedback.",
        "released": "Approved version released. Open Maintenance review in Feedback.",
    }
    sent = False
    try:
        run = review.get_run(run_id)
        if run is None or maintainer.get_job(run["job_uuid"]) is None:
            raise ValueError("Notification review is unavailable.")
        summary = messages[notification["kind"]]
        link = _review_link(run_id)
        if link:
            summary += "\n" + link
        sent = notifier(
            {"uuid": run_id, "feedback_uuid": run_id, "notification_id": notification["id"]},
            status=notification["kind"], summary=summary,
        ) is True
    except Exception:
        sent = False
    with maintainer._connect() as connection:
        current = connection.execute("SELECT attempts, status, lease_until FROM review_notifications WHERE id = ?",
                                     (notification["id"],)).fetchone()
    if (current is None or current["status"] != "sending" or current["attempts"] != notification["attempts"]
            or current["lease_until"] <= review._now()):
        return WorkerResult("Notification lease expired", run_id=run_id)
    if sent:
        review.notification_sent(notification["id"])
    else:
        review.notification_failed(notification["id"], notification["attempts"])
    return WorkerResult("Notified" if sent else "Notification retry", run_id=run_id, notification_sent=sent)


def _role_config(role: str) -> WorkerConfig:
    if role == "generate":
        return WorkerConfig.from_env()
    state = _protected_root(Path(os.environ.get("LUIGI_MAINTAINER_STATE_DIR", "/var/lib/luigi-maintainer")))
    repository = os.environ.get("LUIGI_MAINTAINER_REPOSITORY_URL", "").strip()
    legacy._repository_identity(repository)
    remote = os.environ.get("LUIGI_MAINTAINER_REMOTE", "origin").strip()
    branch = legacy._validate_branch(os.environ.get("LUIGI_MAINTAINER_BASE_BRANCH", "main").strip())
    if not legacy.REMOTE_RE.fullmatch(remote):
        raise ValueError("Invalid configured remote.")
    return WorkerConfig(state, repository, remote=remote, base_branch=branch)


def main(argv: list[str] | None = None) -> int:
    """One role per process. Parent deployment owns credentials and scheduling."""
    parser = argparse.ArgumentParser(description="Run one approval-gated review role.")
    roles = parser.add_mutually_exclusive_group()
    for role in ("generate", "publish", "refresh", "notify", "release"):
        roles.add_argument("--" + role, action="store_const", const=role, dest="role")
    roles.add_argument("--preview", metavar="REVIEW_ID")
    roles.add_argument("--preview-pending", action="store_true")
    roles.add_argument("--stop-preview", action="store_true")
    arguments = parser.parse_args(argv)
    selected = arguments.role or "generate"
    try:
        if arguments.stop_preview:
            from .test_preview import stop_preview

            stop_preview()
            result = WorkerResult("Preview stopped")
        elif arguments.preview or arguments.preview_pending:
            from .test_preview import start_preview, sandbox as preview_sandbox

            if not preview_sandbox.available().available:
                result = WorkerResult("Unavailable")
            else:
                pending = [run for run in review.list_runs(state="testing")
                           if run.get("preview_commit") != run.get("head_commit")]
                run_id = arguments.preview or (pending[0]["id"] if pending else None)
                result = preview_once(_role_config("preview"), run_id, preview_runner=start_preview) if run_id else WorkerResult("Idle")
        elif selected == "notify":
            result = notify_once()
        else:
            functions = {"generate": generation_once, "publish": publish_once, "refresh": refresh_once, "release": release_once}
            result = functions[selected](_role_config(selected))
    except Exception:
        print("Review worker configuration or queue is unavailable.")
        return 2
    print("Review worker status: " + result.status)
    return 2 if result.status == "Unavailable" else 1 if result.status == "needs_attention" else 0


if __name__ == "__main__":
    raise SystemExit(main())
