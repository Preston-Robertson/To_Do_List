"""Administrator review actions; worker receipts are deliberately not HTTP APIs."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
import struct
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID
import zlib

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.routing import APIRoute

from luigi_web.auth import CSRF_COOKIE_NAME, csrf_matches, is_authenticated, require_auth
from luigi_web.core.templating import create_templates

from . import maintainer, review


class ReviewRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def protected_handler(request: Request):
            try:
                response = await handler(request)
            except HTTPException as error:
                response = JSONResponse({"detail": error.detail}, status_code=error.status_code,
                                        headers=error.headers)
            except (OSError, sqlite3.Error):
                response = JSONResponse({"detail": "Review is temporarily unavailable."}, status_code=503)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response

        return protected_handler


def review_enabled() -> bool:
    return os.environ.get("LUIGI_WEB_MAINTAINER_REVIEW_ENABLED", "0").strip() == "1"


def release_enabled() -> bool:
    return os.environ.get("LUIGI_WEB_RELEASE_ENABLED", "0").strip() == "1"


def require_review_admin(request: Request, authenticated: bool = Depends(require_auth)) -> None:
    if request.url.query:
        raise HTTPException(400, "Review requests do not accept query parameters.")
    if request.method in {"GET", "HEAD"}:
        return
    authorization = request.headers.get("authorization")
    bearer = bool(authorization and is_authenticated(None, authorization, None))
    origin = request.headers.get("origin")
    expected_origin = f"{request.url.scheme}://{request.url.netloc}"
    if origin and origin != expected_origin:
        raise HTTPException(403, "Review request verification failed.")
    referer = request.headers.get("referer")
    if not origin and referer:
        try:
            source = urlsplit(referer)
        except ValueError:
            raise HTTPException(403, "Review request verification failed.") from None
        if f"{source.scheme}://{source.netloc}" != expected_origin:
            raise HTTPException(403, "Review request verification failed.")
    if not bearer and (request.headers.get("sec-fetch-site") == "cross-site" or not csrf_matches(
        request.cookies.get(CSRF_COOKIE_NAME), request.headers.get("x-csrf-token"),
    )):
        raise HTTPException(403, "Review request verification failed.")
    if not review_enabled():
        raise HTTPException(403, "Maintenance review actions are disabled by deployment configuration.")


router = APIRouter(route_class=ReviewRoute, dependencies=[Depends(require_review_admin)])
templates = create_templates()
VERSION_PATTERN = r"[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?"
CANCEL_STATES = {"awaiting_approval", "publish_queued", "testing", "release_queued", "needs_attention"}
SCREENSHOTS = {"desktop.png": (1440, 900), "mobile.png": (390, 844)}
STATE_LABELS = {
    "generating": "Generating options", "awaiting_approval": "Awaiting option approval",
    "publish_queued": "Publication queued", "publishing": "Publishing test branch",
    "testing": "Testing", "release_queued": "Release queued", "releasing": "Releasing",
    "released": "Released", "needs_attention": "Needs attention", "failed": "Failed", "cancelled": "Cancelled",
}


def _identifier(value: str) -> str:
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(404, "Review not found.") from None
    if canonical != value:
        raise HTTPException(404, "Review not found.")
    return canonical


def _get_run(run_id: str) -> dict:
    run = review.get_run(_identifier(run_id))
    if run is None:
        raise HTTPException(404, "Review not found.")
    return run


def _run_view(run: dict) -> dict:
    view = {field: run.get(field) for field in (
        "id", "revision", "state", "created_at", "updated_at", "branch", "head_commit",
        "version", "expected_tag", "tag", "selected_option_id", "merge_commit",
    )}
    view["state_label"] = STATE_LABELS.get(run["state"], "Unavailable")
    view["checks_ready"] = bool(run.get("head_commit") and run.get("checks_commit") == run["head_commit"])
    view["preview_ready"] = bool(run.get("head_commit") and run.get("preview_commit") == run["head_commit"]
                                 and run.get("preview_url") == review.preview_path(run["id"]))
    view["preview_path"] = review.preview_path(run["id"]) if view["preview_ready"] else None
    view["release_ready"] = run["state"] == "testing" and view["checks_ready"] and view["preview_ready"]
    return view


def _image_metadata(option: dict, filename: str) -> dict | None:
    size = SCREENSHOTS.get(filename)
    matches = [image for image in option.get("screenshots", [])
               if (image.get("width"), image.get("height")) == size]
    return matches[0] if size and len(matches) == 1 else None


@router.get("/feedback/reviews", response_class=HTMLResponse)
def queue_page(request: Request):
    return templates.TemplateResponse("review_queue.html", {
        "request": request, "page_title": "Maintenance review", "active_nav": "feedback",
        "runs": [_run_view(run) for run in review.list_runs(limit=100)],
        "review_enabled": review_enabled(), "release_enabled": release_enabled(),
    })


@router.get("/feedback/reviews/{run_id}", response_class=HTMLResponse)
def detail_page(request: Request, run_id: str):
    run = _get_run(run_id)
    candidates = []
    for internal in review.list_options(run["id"], limit=3):
        candidate = review.candidate_metadata(internal["id"])
        if candidate is None:
            continue
        for field, limit in (("label", 80), ("summary", 2000), ("notes", 2000)):
            candidate[field] = maintainer.sanitize_output(candidate[field], limit=limit)
        candidate["images"] = [{"filename": filename, "width": dimensions[0], "height": dimensions[1]}
                               for filename, dimensions in SCREENSHOTS.items()
                               if _image_metadata(candidate, filename)]
        candidates.append(candidate)
    return templates.TemplateResponse("review_detail.html", {
        "request": request, "page_title": "Maintenance review", "active_nav": "feedback",
        "run": _run_view(run), "options": candidates, "review_enabled": review_enabled(),
        "release_enabled": release_enabled(), "cancel_allowed": run["state"] in CANCEL_STATES,
        "version_pattern": VERSION_PATTERN,
    })


@router.get("/feedback/reviews/{run_id}/preview/", response_class=HTMLResponse)
def preview_entry(request: Request, run_id: str):
    from . import test_preview

    run = _get_run(run_id)
    if not _run_view(run)["preview_ready"] or run["state"] != "testing":
        raise HTTPException(409, "The approved test application is not ready for this commit.")
    try:
        origin = test_preview.preview_origin()
        ticket = test_preview.create_ticket(run)
    except (ValueError, OSError, KeyError):
        raise HTTPException(503, "The isolated test application is unavailable.") from None
    return templates.TemplateResponse("review_preview.html", {
        "request": request, "page_title": "Open test application", "active_nav": "feedback",
        "run": _run_view(run), "preview_origin": origin, "ticket": ticket,
    }, headers={"Content-Security-Policy": f"form-action {origin}; frame-ancestors 'none'"})


def _artifact_bytes(run_id: str, option_id: str, filename: str, limit: int) -> tuple[bytes, dict]:
    """Finalized files: configured root / run UUID / private artifact UUID / fixed name."""
    _identifier(run_id)
    option = review.get_option(_identifier(option_id))
    if option is None or option["run_id"] != run_id:
        raise HTTPException(404, "Artifact unavailable.")
    root = Path(os.environ.get("LUIGI_MAINTAINER_ARTIFACT_DIR") or
                maintainer.db_path().parent / "review-artifacts").absolute()
    image = _image_metadata(option, filename) if filename in SCREENSHOTS else None
    if filename in SCREENSHOTS and image is None:
        raise HTTPException(404, "Artifact unavailable.")
    artifact_id = image["artifact_id"] if image else option["artifact_id"]
    target = root / run_id / _identifier(artifact_id) / filename
    try:
        if any(
            path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            for path in (target, *target.parents)
        ):
            raise ValueError
        resolved_root = root.resolve(strict=True)
        resolved = target.resolve(strict=True)
        if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
            raise ValueError
        with resolved.open("rb") as stream:
            content = stream.read(limit + 1)
        if not content or len(content) > limit:
            raise ValueError
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(404, "Artifact unavailable.") from None
    return content, option


def _png_dimensions(content: bytes) -> tuple[int, int]:
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError
    position = 8
    dimensions = None
    has_pixels = False
    while position + 12 <= len(content):
        length = struct.unpack_from(">I", content, position)[0]
        kind = content[position + 4:position + 8]
        end = position + 12 + length
        if end > len(content) or kind not in {
            b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"sRGB", b"gAMA", b"cHRM", b"pHYs", b"bKGD", b"sBIT",
        }:
            raise ValueError
        checksum = struct.unpack_from(">I", content, end - 4)[0]
        if zlib.crc32(content[position + 4:end - 4]) != checksum:
            raise ValueError
        if dimensions is None:
            if kind != b"IHDR" or length != 13:
                raise ValueError
            dimensions = struct.unpack_from(">II", content, position + 8)
        elif kind == b"IHDR":
            raise ValueError
        if kind == b"IDAT" and length:
            has_pixels = True
        if kind == b"IEND":
            if length or end != len(content) or not has_pixels:
                raise ValueError
            return dimensions
        position = end
    raise ValueError


def artifact_response(run_id: str, option_id: str, filename: str) -> Response:
    if filename not in SCREENSHOTS:
        raise HTTPException(404, "Artifact unavailable.")
    content, option = _artifact_bytes(run_id, option_id, filename, 8 * 1024 * 1024)
    image = _image_metadata(option, filename)
    try:
        if (image is None or hashlib.sha256(content).hexdigest() != image["sha256"]
                or _png_dimensions(content) != SCREENSHOTS[filename]):
            raise ValueError
    except (ValueError, struct.error):
        raise HTTPException(404, "Artifact unavailable.") from None
    return Response(content, media_type="image/png", headers={
        "Content-Security-Policy": "default-src 'none'; sandbox", "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    })


@router.get("/feedback/reviews/{run_id}/options/{option_id}/artifacts/{filename}")
def screenshot(run_id: str, option_id: str, filename: str):
    return artifact_response(run_id, option_id, filename)


@router.get("/feedback/reviews/{run_id}/options/{option_id}/diff")
def option_diff(run_id: str, option_id: str):
    content, option = _artifact_bytes(run_id, option_id, "candidate.patch", 1_200_000)
    if hashlib.sha256(content).hexdigest() != option["diff_sha256"]:
        raise HTTPException(404, "Artifact unavailable.")
    try:
        content.decode("utf-8", errors="strict")
    except UnicodeError:
        raise HTTPException(404, "Artifact unavailable.") from None
    return Response(content, media_type="text/plain", headers={
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Content-Disposition": 'inline; filename="candidate.patch"',
    })


async def _form(request: Request, fields: set[str], *, confirmation_field: str = "confirm") -> dict[str, str]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        raise HTTPException(400, "Invalid review form.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 16 * 1024:
            raise HTTPException(413, "Review form is too large.")
    try:
        pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True,
                          encoding="utf-8", errors="strict", max_num_fields=10)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid review form.") from None
    form = dict(pairs)
    if len(pairs) != len(form) or set(form) != fields or form.get(confirmation_field) != "on":
        raise HTTPException(400, "Review every field and confirm this action.")
    if not re.fullmatch(r"[1-9][0-9]{0,9}", form["expected_revision"]):
        raise HTTPException(400, "Invalid review form.")
    if "version" in fields and (len(form["version"]) > 8 or not re.fullmatch(VERSION_PATTERN, form["version"])):
        raise HTTPException(400, "Enter an exact numeric X.Y or X.Y.Z version.")
    return form


def _fresh(run: dict, form: dict[str, str]) -> int:
    revision = int(form["expected_revision"])
    if revision != run["revision"]:
        raise HTTPException(412, "This review changed. Reload and review it again; nothing was queued.")
    return revision


def _conflict() -> HTTPException:
    return HTTPException(409, "This action is no longer available. Reload and review again; nothing was queued.")


def _action_response(request: Request, run_id: str) -> Response:
    location = f"/feedback/reviews/{run_id}"
    if "application/json" in request.headers.get("accept", "").lower():
        return JSONResponse({"location": location})
    return RedirectResponse(location, status_code=303)


@router.post("/feedback/reviews/{run_id}/approve")
async def approve(request: Request, run_id: str):
    form = await _form(request, {"option_id", "expected_revision", "version", "confirm"})
    run = _get_run(run_id)
    revision = _fresh(run, form)
    option_id = _identifier(form["option_id"])
    try:
        review.approve_option(run["id"], option_id, revision, form["version"])
    except ValueError:
        raise _conflict() from None
    return _action_response(request, run["id"])


@router.post("/feedback/reviews/{run_id}/release")
async def release(request: Request, run_id: str):
    if not release_enabled():
        raise HTTPException(403, "Release actions are disabled by deployment configuration.")
    form = await _form(request, {"expected_revision", "head_commit", "version", "confirmed"},
                       confirmation_field="confirmed")
    run = _get_run(run_id)
    revision = _fresh(run, form)
    try:
        from .test_preview import verify_ready

        verify_ready(run)
        review.approve_release(run["id"], revision, form["head_commit"], form["version"], confirmed=True)
    except (ValueError, OSError):
        raise _conflict() from None
    return _action_response(request, run["id"])


@router.post("/feedback/reviews/{run_id}/cancel")
async def cancel(request: Request, run_id: str):
    form = await _form(request, {"expected_revision", "confirm"})
    run = _get_run(run_id)
    revision = _fresh(run, form)
    try:
        review.cancel_run(run["id"], revision)
    except ValueError:
        raise _conflict() from None
    return _action_response(request, run["id"])
