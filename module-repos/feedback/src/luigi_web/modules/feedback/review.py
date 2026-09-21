"""Durable review state in the approved maintainer queue, never the raw inbox.

All returned records are detached dictionaries. Admin entry points require an
exact revision; the parent routes must authenticate the admin and enforce CSRF.
Worker entry points are internal receipts, not browser actions or a sandbox.
Only trusted workers may supply artifacts, test results, or commit identities.
The store performs no network, Git, deployment, shell, or email operations.

Parent integration contract:
* Keep enqueue_feedback as the explicit privacy approval boundary. Claim that
    job Running, then create_run -> add_option (2-3) -> finish_generation.
* Admin approve_option requires the displayed revision and literal version.
* Worker claim_publish -> record_published -> record_preview/record_test_result.
* Admin approve_release requires revision, HEAD, exact version, confirmed=True.
* Worker claim_release -> record_released, or mark_attention on uncertainty.
* record_head is an explicit worker refresh, never a deployment or retry.
* notifications_due is read-only; claim_notification reserves an email attempt;
    notification_sent acknowledges delivery; notification_failed schedules retry.

The parent resolves repository_id='host' from protected configuration, checks
remote tag availability (no default version), verifies artifacts against the
approved digests, enforces CI/protected-branch policy, and owns job completion.
No function here grants worker permissions to call admin routes. Worker receipt
functions MUST NOT be exposed as GUI actions. Public option views must use
candidate_metadata, not the private artifact ID returned by get/list_options.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import timedelta, timezone
from typing import Any, Generator

from luigi_web import clock
from . import maintainer


STATES = (
    "generating", "awaiting_approval", "publish_queued", "publishing", "testing",
    "release_queued", "releasing", "released", "needs_attention", "failed",
    "cancelled",
)
REQUIRED_CHECKS = frozenset({"unittest", "validator", "design_review"})
CHECK_IDS = REQUIRED_CHECKS | {"diff_check"}
MAX_NOTIFICATION_ATTEMPTS = 5
NOTIFICATION_LEASE_SECONDS = 300


def _now() -> str:
    return _later(0)


def _later(seconds: int) -> str:
    return (clock.local_now().astimezone(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def _identifier(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid UUID") from None


def _commit(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
        raise ValueError("invalid commit SHA")
    return value


def _digest(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid SHA256 digest")
    return value


def _text(value: str, *, limit: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("summary must be text")
    text = maintainer.sanitize_output(value, limit=limit).strip()
    if required and not text:
        raise ValueError("summary is required")
    return text


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("limit must be 1-100")
    return value


def init_db() -> None:
    """Add only review-owned tables; leave the existing job schema/statuses alone."""
    maintainer.init_db()
    with maintainer._connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS review_runs (
                id TEXT PRIMARY KEY,
                job_uuid TEXT NOT NULL UNIQUE REFERENCES maintainer_jobs(uuid),
                repository_id TEXT NOT NULL DEFAULT 'host' CHECK(repository_id = 'host'),
                revision INTEGER NOT NULL CHECK(revision > 0),
                state TEXT NOT NULL CHECK(state IN (
                    'generating', 'awaiting_approval', 'publish_queued', 'publishing',
                    'testing', 'release_queued', 'releasing', 'released',
                    'needs_attention', 'failed', 'cancelled'
                )),
                options_requested INTEGER NOT NULL CHECK(options_requested BETWEEN 2 AND 3),
                base_commit TEXT NOT NULL,
                selected_option_id TEXT REFERENCES review_options(id),
                approved_diff_sha256 TEXT,
                approved_validation_digest TEXT,
                branch TEXT NOT NULL UNIQUE,
                head_commit TEXT,
                preview_commit TEXT,
                preview_url TEXT,
                checks_commit TEXT,
                version TEXT,
                expected_tag TEXT,
                pr_number INTEGER,
                pr_url TEXT,
                release_approved_head TEXT,
                release_approved_version TEXT,
                release_approved_at TEXT,
                merge_commit TEXT,
                tag TEXT,
                error_detail TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS review_options (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES review_runs(id),
                label TEXT NOT NULL,
                summary TEXT NOT NULL,
                notes TEXT NOT NULL,
                diff_sha256 TEXT NOT NULL,
                tree_sha256 TEXT,
                artifact_id TEXT NOT NULL,
                test_results TEXT NOT NULL,
                screenshots TEXT NOT NULL,
                tests_passed INTEGER NOT NULL CHECK(tests_passed IN (0, 1)),
                validation_digest TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, diff_sha256)
            )
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS review_options_no_update
            BEFORE UPDATE ON review_options BEGIN
                SELECT RAISE(ABORT, 'review options are immutable');
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS review_options_no_delete
            BEFORE DELETE ON review_options BEGIN
                SELECT RAISE(ABORT, 'review options are immutable');
            END
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS review_events (
                id INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES review_runs(id),
                revision INTEGER NOT NULL,
                actor_role TEXT NOT NULL CHECK(actor_role IN ('admin', 'worker')),
                action TEXT NOT NULL,
                state TEXT NOT NULL,
                metadata TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, revision)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS review_notifications (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES review_runs(id),
                revision INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('awaiting_approval', 'testing', 'released')),
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'sending', 'sent', 'exhausted')),
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at TEXT NOT NULL,
                lease_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(run_id, revision, kind)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS review_runs_queue ON review_runs(state, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS review_notifications_queue ON review_notifications(status, created_at)")


@contextmanager
def _transaction() -> Generator[sqlite3.Connection, None, None]:
    init_db()
    with maintainer._connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        yield conn


def _run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM review_runs WHERE id = ?", (_identifier(run_id),)).fetchone()
    if row is None:
        raise ValueError("review run not found")
    return dict(row)


def _state(run: dict[str, Any], *allowed: str) -> None:
    if run["state"] not in allowed:
        raise ValueError("review state does not allow this action")


def _revision(run: dict[str, Any], expected_revision: int) -> None:
    if type(expected_revision) is not int or run["revision"] != expected_revision:
        raise ValueError("stale review revision")


def _event(conn: sqlite3.Connection, run: dict[str, Any], actor: str, action: str) -> None:
    metadata = {field: run[field] for field in (
        "repository_id", "selected_option_id", "approved_diff_sha256", "approved_validation_digest",
        "base_commit", "head_commit", "checks_commit", "preview_commit", "version", "expected_tag",
        "pr_number", "release_approved_head", "release_approved_version", "merge_commit", "tag",
    )}
    conn.execute("""
        INSERT INTO review_events (run_id, revision, actor_role, action, state, metadata, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (run["id"], run["revision"], actor, action, run["state"], _json(metadata), run["updated_at"]))


def _change(
    conn: sqlite3.Connection, run: dict[str, Any], *, actor: str, action: str,
    notify: bool = False, **fields: Any,
) -> dict[str, Any]:
    fields.update(revision=run["revision"] + 1, updated_at=_now())
    assignments = ", ".join(f"{field} = ?" for field in fields)
    conn.execute(f"UPDATE review_runs SET {assignments} WHERE id = ?", (*fields.values(), run["id"]))
    updated = _run(conn, run["id"])
    _event(conn, updated, actor, action)
    if notify:
        conn.execute("""
                        INSERT INTO review_notifications (id, run_id, revision, kind, available_at, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (str(uuid.uuid4()), updated["id"], updated["revision"], updated["state"],
                            updated["updated_at"], updated["updated_at"], updated["updated_at"]))
    return updated


def create_run(job_uuid: str, base_commit: str, options_requested: int = 3) -> dict[str, Any]:
    """Worker: create once after claim_next_job; repository is the configured host."""
    job_uuid = _identifier(job_uuid)
    base_commit = _commit(base_commit)
    if type(options_requested) is not int or options_requested not in (2, 3):
        raise ValueError("request two or three options")
    job = maintainer.get_job(job_uuid)
    if job is None or job["status"] != "Running":
        raise ValueError("approved maintainer job must be Running")
    run_id = str(uuid.uuid4())
    now = _now()
    with _transaction() as conn:
        current = conn.execute("SELECT status FROM maintainer_jobs WHERE uuid = ?", (job_uuid,)).fetchone()
        if current is None or current["status"] != "Running":
            raise ValueError("approved maintainer job must be Running")
        if conn.execute("SELECT 1 FROM review_runs WHERE job_uuid = ?", (job_uuid,)).fetchone():
            raise ValueError("maintainer job already has a review run")
        conn.execute("""
            INSERT INTO review_runs (id, job_uuid, revision, state, options_requested,
                                     base_commit, branch, created_at, updated_at)
            VALUES (?, ?, 1, 'generating', ?, ?, ?, ?, ?)
        """, (run_id, job_uuid, options_requested, base_commit,
              f"automation/review-{uuid.UUID(run_id).hex}", now, now))
        run = _run(conn, run_id)
        _event(conn, run, "worker", "created")
    return run


def get_run(run_id: str) -> dict[str, Any] | None:
    init_db()
    with maintainer._connect() as conn:
        row = conn.execute("SELECT * FROM review_runs WHERE id = ?", (_identifier(run_id),)).fetchone()
    return dict(row) if row else None


def list_runs(*, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    limit = _limit(limit)
    if state is not None and state not in STATES:
        raise ValueError("invalid review state")
    init_db()
    with maintainer._connect() as conn:
        rows = conn.execute("""
            SELECT * FROM review_runs WHERE (? IS NULL OR state = ?)
            ORDER BY created_at, rowid LIMIT ?
        """, (state, state, limit)).fetchall()
    return [dict(row) for row in rows]


def list_events(run_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    limit = _limit(limit)
    init_db()
    with maintainer._connect() as conn:
        rows = conn.execute("""
            SELECT * FROM review_events WHERE run_id = ? ORDER BY revision DESC LIMIT ?
        """, (_identifier(run_id), limit)).fetchall()
    return [{**dict(row), "metadata": json.loads(row["metadata"])} for row in rows]


def _test_metadata(results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(results, list) or len(results) > len(CHECK_IDS):
        raise ValueError("invalid test results")
    normalized = []
    seen = set()
    for result in results:
        if not isinstance(result, dict) or set(result) != {
            "command_id", "passed", "total", "failed", "skipped", "output_digest",
        }:
            raise ValueError("test results accept only bounded metadata, not logs")
        command = result["command_id"]
        if not isinstance(command, str) or command not in CHECK_IDS or command in seen:
            raise ValueError("invalid or duplicate command ID")
        seen.add(command)
        if type(result["passed"]) is not bool:
            raise ValueError("test outcome must be boolean")
        for field in ("total", "failed", "skipped"):
            if type(result[field]) is not int or not 0 <= result[field] <= 1_000_000:
                raise ValueError("invalid test count")
        if result["failed"] + result["skipped"] > result["total"]:
            raise ValueError("inconsistent test counts")
        if result["passed"] and (result["failed"] or result["skipped"] or not result["total"]):
            raise ValueError("passing checks require executed tests without failures or skips")
        normalized.append({**result, "output_digest": _digest(result["output_digest"])})
    normalized.sort(key=lambda item: item["command_id"])
    return normalized, REQUIRED_CHECKS <= seen and all(item["passed"] for item in normalized)


def _screenshot_metadata(screenshots: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if screenshots is None:
        return []
    if not isinstance(screenshots, list) or len(screenshots) > 8:
        raise ValueError("invalid screenshot metadata")
    normalized = []
    seen = set()
    for item in screenshots:
        if not isinstance(item, dict) or set(item) != {"artifact_id", "sha256", "width", "height"}:
            raise ValueError("screenshots accept opaque IDs, not paths or URLs")
        artifact_id = _identifier(item["artifact_id"])
        if artifact_id in seen:
            raise ValueError("duplicate screenshot ID")
        seen.add(artifact_id)
        for dimension in ("width", "height"):
            if type(item[dimension]) is not int or not 1 <= item[dimension] <= 8192:
                raise ValueError("invalid screenshot dimensions")
        normalized.append({**item, "artifact_id": artifact_id, "sha256": _digest(item["sha256"])})
    return sorted(normalized, key=lambda item: item["artifact_id"])


def _option(row: sqlite3.Row) -> dict[str, Any]:
    option = dict(row)
    option["tests_passed"] = bool(option["tests_passed"])
    option["test_results"] = json.loads(option["test_results"])
    option["screenshots"] = json.loads(option["screenshots"])
    return option


def add_option(
    run_id: str, *, label: str, summary: str, diff_sha256: str, artifact_id: str,
    test_results: list[dict[str, Any]], tree_sha256: str | None = None,
    notes: str = "", screenshots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Worker: immutable metadata only. unittest means the full suite, not a subset.

    Required passing receipts are unittest, validator, and design_review; checks
    with skips are not passing. Design review covers synthetic screenshots when
    UI changes apply. Screenshot IDs resolve only through the parent's host API.
    Artifact IDs identify private worker storage, never filenames or paths.
    validation_digest is computed here from the canonical artifact/test metadata.
    """
    results, passed = _test_metadata(test_results)
    images = _screenshot_metadata(screenshots)
    evidence = {
        "diff_sha256": _digest(diff_sha256),
        "tree_sha256": _digest(tree_sha256) if tree_sha256 is not None else None,
        "artifact_id": _identifier(artifact_id), "test_results": results, "screenshots": images,
    }
    label = _text(label, limit=80, required=True)
    summary = _text(summary, limit=2000, required=True)
    notes = _text(notes, limit=2000)
    option_id = str(uuid.uuid4())
    validation_digest = hashlib.sha256(_json(evidence).encode("ascii")).hexdigest()
    with _transaction() as conn:
        run = _run(conn, run_id)
        _state(run, "generating")
        count = conn.execute("SELECT COUNT(*) FROM review_options WHERE run_id = ?", (run["id"],)).fetchone()[0]
        if count >= run["options_requested"]:
            raise ValueError("requested option limit reached")
        if conn.execute("SELECT 1 FROM review_options WHERE run_id = ? AND diff_sha256 = ?",
                        (run["id"], evidence["diff_sha256"])).fetchone():
            raise ValueError("duplicate option patch")
        conn.execute("""
            INSERT INTO review_options (id, run_id, label, summary, notes, diff_sha256,
                tree_sha256, artifact_id, test_results, screenshots, tests_passed,
                validation_digest, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (option_id, run["id"], label, summary, notes, evidence["diff_sha256"],
              evidence["tree_sha256"], evidence["artifact_id"], _json(results), _json(images),
              int(passed), validation_digest, _now()))
        _change(conn, run, actor="worker", action="option_added")
        option = _option(conn.execute("SELECT * FROM review_options WHERE id = ?", (option_id,)).fetchone())
    return option


def get_option(option_id: str) -> dict[str, Any] | None:
    """Internal metadata, including the opaque private patch artifact ID."""
    init_db()
    with maintainer._connect() as conn:
        row = conn.execute("SELECT * FROM review_options WHERE id = ?", (_identifier(option_id),)).fetchone()
    return _option(row) if row else None


def list_options(run_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    limit = _limit(limit)
    init_db()
    with maintainer._connect() as conn:
        rows = conn.execute("""
            SELECT * FROM review_options WHERE run_id = ? ORDER BY created_at, rowid LIMIT ?
        """, (_identifier(run_id), limit)).fetchall()
    return [_option(row) for row in rows]


def candidate_metadata(option_id: str) -> dict[str, Any] | None:
    """Browser-safe metadata; excludes the private patch artifact and any source."""
    option = get_option(option_id)
    if option is None:
        return None
    return {field: option[field] for field in (
        "id", "run_id", "label", "summary", "notes", "diff_sha256", "tests_passed",
        "validation_digest", "test_results", "screenshots", "created_at",
    )}


def finish_generation(run_id: str) -> dict[str, Any]:
    """Worker: two distinct passing patches are required for an admin decision."""
    with _transaction() as conn:
        run = _run(conn, run_id)
        _state(run, "generating")
        passed = conn.execute("""
            SELECT COUNT(DISTINCT diff_sha256) FROM review_options
            WHERE run_id = ? AND tests_passed = 1
        """, (run["id"],)).fetchone()[0]
        ready = passed >= 2
        result = _change(
            conn, run, actor="worker", action="generation_finished", notify=ready,
            state="awaiting_approval" if ready else "needs_attention",
            error_detail=None if ready else "At least two distinct passing options are required.",
        )
    return result


def _version(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?", value):
        raise ValueError("version must be exact numeric X.Y or X.Y.Z")
    return value


def _selected(conn: sqlite3.Connection, run: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM review_options WHERE id = ? AND run_id = ?",
                       (run["selected_option_id"], run["id"])).fetchone()
    if row is None or not row["tests_passed"]:
        raise ValueError("selected option must belong to this run and pass validation")
    if (row["diff_sha256"] != run["approved_diff_sha256"]
            or row["validation_digest"] != run["approved_validation_digest"]):
        raise ValueError("selected option approval binding changed")
    if run["expected_tag"] != f"v{_version(run['version'])}":
        raise ValueError("approved version binding changed")
    return _option(row)


def approve_option(
    run_id: str, option_id: str, expected_revision: int, version: str,
) -> dict[str, Any]:
    """Admin: bind one passing candidate and the literal version; never auto-bump."""
    version = _version(version)
    option_id = _identifier(option_id)
    with _transaction() as conn:
        run = _run(conn, run_id)
        _revision(run, expected_revision)
        _state(run, "awaiting_approval")
        option = conn.execute("SELECT * FROM review_options WHERE id = ? AND run_id = ?",
                              (option_id, run["id"])).fetchone()
        if option is None or not option["tests_passed"]:
            raise ValueError("option must belong to this run and pass validation")
        result = _change(
            conn, run, actor="admin", action="option_approved", state="publish_queued",
            selected_option_id=option_id, approved_diff_sha256=option["diff_sha256"],
            approved_validation_digest=option["validation_digest"], version=version,
            expected_tag=f"v{version}", error_detail=None,
        )
    return result


def _release_ready(run: dict[str, Any]) -> None:
    head = run["head_commit"]
    if not head or run["checks_commit"] != head or run["preview_commit"] != head:
        raise ValueError("current HEAD needs passing checks and verified preview")


def _release_authorized(run: dict[str, Any]) -> None:
    _release_ready(run)
    if (run["release_approved_head"] != run["head_commit"]
            or run["release_approved_version"] != run["version"]
            or not run["release_approved_at"]):
        raise ValueError("release requires exact admin authorization")


def _claim(queued: str, active: str) -> dict[str, Any] | None:
    with _transaction() as conn:
        row = conn.execute("SELECT * FROM review_runs WHERE state = ? ORDER BY created_at, rowid LIMIT 1",
                           (queued,)).fetchone()
        if row is None:
            return None
        run = dict(row)
        _selected(conn, run)
        if active == "releasing":
            _release_authorized(run)
        claimed = _change(conn, run, actor="worker", action=f"{active}_claimed", state=active)
    return claimed


def claim_publish() -> dict[str, Any] | None:
    """Worker: claim once. Ambiguous external outcomes must go to attention, not retry."""
    return _claim("publish_queued", "publishing")


def record_published(
    run_id: str, head_commit: str, branch: str, pr_url: str, pr_number: int,
) -> dict[str, Any]:
    """Worker receipt after verifying the configured host repository and fixed branch."""
    head_commit = _commit(head_commit)
    if type(pr_number) is not int or not 1 <= pr_number <= 2_147_483_647:
        raise ValueError("invalid pull request number")
    if not isinstance(pr_url, str) or not re.fullmatch(
        rf"https://github\.com/[A-Za-z0-9_-]{{1,100}}/[A-Za-z0-9_.-]{{1,100}}/pull/{pr_number}", pr_url,
    ):
        raise ValueError("pull request URL must be a canonical GitHub pull request")
    with _transaction() as conn:
        run = _run(conn, run_id)
        _state(run, "publishing")
        _selected(conn, run)
        if branch != run["branch"]:
            raise ValueError("branch must be the fixed review branch")
        result = _change(
            conn, run, actor="worker", action="published", state="testing", notify=True,
            head_commit=head_commit, pr_number=pr_number, pr_url=pr_url,
        )
    return result


def _head(run: dict[str, Any], head_commit: str) -> None:
    if _commit(head_commit) != run["head_commit"]:
        raise ValueError("receipt does not match current HEAD")


def preview_path(run_id: str) -> str:
    """Fixed same-origin reverse-proxy location, never a worker/container address."""
    return f"/feedback/reviews/{_identifier(run_id)}/preview/"


def record_preview(run_id: str, head_commit: str, preview_url: str) -> dict[str, Any]:
    """Worker only, after the isolated preview reports readiness at this exact SHA."""
    with _transaction() as conn:
        run = _run(conn, run_id)
        _state(run, "testing")
        _head(run, head_commit)
        if preview_url != preview_path(run["id"]):
            raise ValueError("preview URL must be the fixed same-origin review path")
        result = _change(conn, run, actor="worker", action="preview_verified",
                         preview_commit=head_commit, preview_url=preview_url)
    return result


def record_test_result(run_id: str, head_commit: str, passed: bool) -> dict[str, Any]:
    """Worker receipt for full checks at HEAD; never substitutes for preview readiness."""
    if type(passed) is not bool:
        raise ValueError("test outcome must be boolean")
    with _transaction() as conn:
        run = _run(conn, run_id)
        _state(run, "testing", "release_queued", "releasing")
        _head(run, head_commit)
        if passed and run["state"] != "testing":
            _release_authorized(run)
            return run
        if passed:
            result = _change(conn, run, actor="worker", action="checks_recorded", checks_commit=head_commit)
        else:
            ambiguous = run["state"] == "releasing"
            result = _change(
                conn, run, actor="worker", action="checks_failed", checks_commit=None, **_clear_release(),
                state="needs_attention" if ambiguous else "testing",
                error_detail="Checks failed during release; verify the external outcome." if ambiguous else None,
            )
    return result


def _clear_release() -> dict[str, Any]:
    return {"release_approved_head": None, "release_approved_version": None, "release_approved_at": None}


def record_head(run_id: str, head_commit: str) -> dict[str, Any]:
    """Worker refresh: changed HEAD revokes all preview/check/release evidence.

    During a release, an observed change is ambiguous and requires attention;
    it must never automatically queue another merge or release attempt.
    """
    head_commit = _commit(head_commit)
    with _transaction() as conn:
        run = _run(conn, run_id)
        _state(run, "testing", "release_queued", "releasing", "needs_attention")
        if not run["head_commit"]:
            raise ValueError("review has not been published")
        if head_commit == run["head_commit"]:
            return run
        ambiguous = run["state"] in {"releasing", "needs_attention"}
        result = _change(
            conn, run, actor="worker", action="head_changed",
            state="needs_attention" if ambiguous else "testing", head_commit=head_commit,
            checks_commit=None, preview_commit=None, preview_url=None, **_clear_release(),
            error_detail="HEAD changed; verify the external outcome before proceeding." if ambiguous else None,
        )
    return result


def approve_release(
    run_id: str, expected_revision: int, head_commit: str, version: str, confirmed: bool = False,
) -> dict[str, Any]:
    """Admin: explicit 'Tested this commit' confirmation, bound to revision/SHA/version."""
    version = _version(version)
    if confirmed is not True:
        raise ValueError("explicit Tested this commit confirmation is required")
    with _transaction() as conn:
        run = _run(conn, run_id)
        _revision(run, expected_revision)
        _state(run, "testing")
        _head(run, head_commit)
        _selected(conn, run)
        _release_ready(run)
        if version != run["version"]:
            raise ValueError("release version must match the exact approved version")
        result = _change(
            conn, run, actor="admin", action="release_approved", state="release_queued",
            release_approved_head=head_commit, release_approved_version=version,
            release_approved_at=_now(),
        )
    return result


def claim_release() -> dict[str, Any] | None:
    """Worker: claim once; independently recheck remote HEAD/CI/tag before any merge."""
    return _claim("release_queued", "releasing")


def record_released(run_id: str, merge_commit: str, tag: str) -> dict[str, Any]:
    """Worker receipt after verifying merge and exact v{version} tag in the host repo."""
    merge_commit = _commit(merge_commit)
    with _transaction() as conn:
        run = _run(conn, run_id)
        _state(run, "releasing")
        _selected(conn, run)
        _release_authorized(run)
        if tag != run["expected_tag"]:
            raise ValueError("released tag must match the exact approved version")
        result = _change(conn, run, actor="worker", action="released", state="released",
                         merge_commit=merge_commit, tag=tag, notify=True)
    return result


def _stop(run_id: str, state: str, detail: str) -> dict[str, Any]:
    detail = _text(detail, limit=1000) or "Worker action requires review."
    with _transaction() as conn:
        run = _run(conn, run_id)
        if run["state"] in {"released", "failed", "cancelled"}:
            raise ValueError("terminal review cannot be changed")
        result = _change(conn, run, actor="worker", action=state, state=state,
                         error_detail=detail, **_clear_release())
    return result


def mark_attention(run_id: str, detail: str = "") -> dict[str, Any]:
    """Worker: sanitized policy-safe summary only, never an exception, code, or raw logs."""
    return _stop(run_id, "needs_attention", detail)


def fail_run(run_id: str, detail: str = "") -> dict[str, Any]:
    """Worker: permanent failure with a sanitized, bounded policy-safe summary."""
    return _stop(run_id, "failed", detail)


def cancel_run(run_id: str, expected_revision: int) -> dict[str, Any]:
    """Admin: cancel only when no generation, publish, or release is in flight."""
    with _transaction() as conn:
        run = _run(conn, run_id)
        _revision(run, expected_revision)
        _state(run, "awaiting_approval", "publish_queued", "testing", "release_queued", "needs_attention")
        result = _change(conn, run, actor="admin", action="cancelled", state="cancelled", **_clear_release())
    return result


def notifications_due(*, limit: int = 100) -> list[dict[str, Any]]:
    """Read-only outbox metadata, with no recipient, message body, or approved request."""
    limit = _limit(limit)
    init_db()
    now = _now()
    with maintainer._connect() as conn:
        rows = conn.execute("""
            SELECT * FROM review_notifications WHERE attempts < ? AND (
                (status = 'pending' AND available_at <= ?)
                OR (status = 'sending' AND lease_until <= ?)
            ) ORDER BY created_at, rowid LIMIT ?
        """, (MAX_NOTIFICATION_ATTEMPTS, now, now, limit)).fetchall()
    return [dict(row) for row in rows]


def claim_notification() -> dict[str, Any] | None:
    """Worker: bounded at-least-once email delivery, with a five-minute crash lease.

    Only email is retried after expiry; publish/release never use this mechanism.
    Use the immutable notification ID as the delivery idempotency key when the
    mail transport supports it. A crash after delivery can cause a duplicate.
    """
    with _transaction() as conn:
        now = _now()
        conn.execute("""
            UPDATE review_notifications SET status = 'exhausted', lease_until = NULL, updated_at = ?
            WHERE status = 'sending' AND attempts >= ? AND lease_until <= ?
        """, (now, MAX_NOTIFICATION_ATTEMPTS, now))
        row = conn.execute("""
            SELECT * FROM review_notifications WHERE attempts < ? AND (
                (status = 'pending' AND available_at <= ?)
                OR (status = 'sending' AND lease_until <= ?)
            ) ORDER BY created_at, rowid LIMIT 1
        """, (MAX_NOTIFICATION_ATTEMPTS, now, now)).fetchone()
        if row is None:
            return None
        lease_until = _later(NOTIFICATION_LEASE_SECONDS)
        conn.execute("""
            UPDATE review_notifications SET status = 'sending', attempts = attempts + 1,
                lease_until = ?, updated_at = ? WHERE id = ?
        """, (lease_until, now, row["id"]))
        claimed = _notification(conn, row["id"])
    return claimed


def _notification(conn: sqlite3.Connection, notification_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM review_notifications WHERE id = ?",
                       (_identifier(notification_id),)).fetchone()
    if row is None:
        raise ValueError("notification not found")
    return dict(row)


def notification_sent(notification_id: str) -> dict[str, Any]:
    """Worker: idempotent delivery acknowledgement, never a send operation."""
    with _transaction() as conn:
        notification = _notification(conn, notification_id)
        if notification["status"] == "sent":
            return notification
        if notification["status"] != "sending":
            raise ValueError("notification must be claimed before acknowledgement")
        conn.execute("""
            UPDATE review_notifications SET status = 'sent', lease_until = NULL, updated_at = ? WHERE id = ?
        """, (_now(), notification["id"]))
        sent = _notification(conn, notification["id"])
    return sent


def notification_failed(notification_id: str, expected_attempt: int) -> dict[str, Any]:
    """Worker: retry email only; reject stale failures and retain no SMTP error text."""
    with _transaction() as conn:
        notification = _notification(conn, notification_id)
        if (type(expected_attempt) is not int or notification["attempts"] != expected_attempt
                or notification["status"] != "sending"):
            raise ValueError("stale notification attempt")
        retry = notification["attempts"] < MAX_NOTIFICATION_ATTEMPTS
        available_at = _later(30 * 2 ** (notification["attempts"] - 1))
        conn.execute("""
            UPDATE review_notifications SET status = ?, available_at = ?, lease_until = NULL,
                updated_at = ? WHERE id = ?
        """, ("pending" if retry else "exhausted", available_at, _now(), notification["id"]))
        failed = _notification(conn, notification["id"])
    return failed