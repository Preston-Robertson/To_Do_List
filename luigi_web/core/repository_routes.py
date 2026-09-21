"""Authenticated, queue-only repository management, independent of Admin."""
from __future__ import annotations

import re
from typing import cast
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from packaging.utils import canonicalize_name, parse_wheel_filename
from starlette.concurrency import run_in_threadpool

from .. import auth
from . import module_repositories as repositories
from .templating import create_templates

router = APIRouter(dependencies=[Depends(auth.require_auth)])
templates = create_templates()
_BASE = "/modules/repositories"
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\.whl\Z")
_UNAVAILABLE = "Repository management is temporarily unavailable. No result could be verified."
_INVALID = "Request not accepted. Check the approved source, release fields, and deployment policy."
_STATES = {"pending": "Queued", "installing": "Installing", "installed": "Installed", "failed": "Failed"}


class _Rejected(ValueError):
    def __init__(self, message: str, status_code: int = 422):
        self.message = message
        self.status_code = status_code


def _origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
            or any(ord(character) <= 32 for character in value) or any(character in value for character in "\\?#")):
        raise ValueError
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


async def _form(request: Request, allowed: set[str]) -> dict[str, str]:
    try:
        supplied_origin = request.headers.get("origin")
        if supplied_origin is not None and _origin(supplied_origin) != _origin(str(request.base_url).rstrip("/")):
            raise ValueError
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise ValueError
    except ValueError:
        raise _Rejected("Same-origin request required.", 403) from None
    bearer = auth.is_authenticated(None, request.headers.get("authorization"), None)
    if not bearer and not auth.is_authenticated(request.cookies.get(auth.COOKIE_NAME), None, None):
        raise _Rejected("A browser session or valid bearer token is required.", 403)
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/x-www-form-urlencoded":
        raise _Rejected("Use the repository form fields.", 415)
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdecimal() or int(declared) > 16384):
        raise _Rejected("Repository request is too large.", 413)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 16384:
            raise _Rejected("Repository request is too large.", 413)
        body.extend(chunk)
    try:
        pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=20, errors="strict")
    except (UnicodeError, ValueError):
        raise _Rejected("Invalid repository form.", 422) from None
    form = dict(pairs)
    if len(pairs) != len(form) or set(form) - allowed - {"csrf_token", "trust_confirmed"}:
        raise _Rejected("Invalid repository form.", 422)
    try:
        csrf_valid = bearer or auth.csrf_matches(
            request.cookies.get(auth.CSRF_COOKIE_NAME), request.headers.get("x-csrf-token") or form.get("csrf_token")
        )
    except TypeError:
        csrf_valid = False
    if not csrf_valid:
        raise _Rejected("CSRF validation failed.", 403)
    if form.get("trust_confirmed") != "yes":
        raise _Rejected("Confirm that you trust this executable module code.")
    return form


def _render(request: Request, *, notice: str = "", error: str = "", status_code: int = 200,
            load: bool = True, setup: bool = False) -> Response:
    policy = {"enabled": False, "registration_enabled": False, "owners": []}
    sources, jobs = [], []
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        registry = getattr(request.app.state, "modules", None)
    current = {module.id for module in getattr(registry, "catalog", ())}
    enabled = {module.id for module in getattr(registry, "enabled", ())}
    if load and not setup:
        try:
            policy = repositories.repository_policy()
            approved = repositories.list_repositories()
            candidates = {record["module_id"]: record for record in repositories.installed_modules()}
            fixed = {source["id"] for source in cast(list[dict[str, str]], policy["policy_sources"])}
            for source in approved:
                candidate = candidates.get(source["module_id"])
                sources.append({
                    **{key: source[key] for key in ("id", "module_id", "repository", "distribution", "package")},
                    "fixed": source["id"] in fixed,
                    "releases": repositories.list_releases(source["id"]),
                    "candidate": {key: candidate[key] for key in ("version", "sha256")} if candidate else None,
                    "running": ("Enabled" if source["module_id"] in enabled else "Disabled")
                    if source["module_id"] in current else "Not loaded",
                })
            for job in repositories.list_jobs():
                jobs.append({
                    **{key: job[key] for key in ("id", "tag", "asset", "sha256", "state")},
                    "source_id": job["source"]["id"], "label": _STATES[job["state"]],
                    "error": "Installation failed; the previous release was preserved." if job["error"] else "",
                })
        except Exception:
            sources, jobs = [], []
            policy = {"enabled": False, "registration_enabled": False, "owners": []}
            error, notice, status_code = _UNAVAILABLE, "", 503
    response = templates.TemplateResponse("module_repositories.html", {
        "request": request, "page_title": "Module repositories", "active_nav": "modules",
        "policy": policy, "sources": sources, "jobs": jobs, "notice": notice, "error": error,
        "setup": setup, "loaded": load and status_code != 503,
        "csrf_value": request.cookies.get(auth.CSRF_COOKIE_NAME, ""),
    }, status_code=status_code)
    response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})
    return response


@router.get(_BASE, response_class=HTMLResponse)
def repositories_page(request: Request) -> Response:
    return _render(request)


@router.get(_BASE + "/setup", response_class=HTMLResponse)
def repository_setup(request: Request) -> Response:
    return _render(request, setup=True)


def _save(request: Request, action: str, form: dict[str, str]) -> Response:
    try:
        policy = repositories.repository_policy()
        if not policy["enabled"]:
            raise _Rejected("Repository installation is disabled by deployment policy.", 403)
        if action == "register":
            if not policy["registration_enabled"]:
                raise _Rejected("Source registration is disabled by deployment policy.", 403)
            if not all(_IDENTIFIER.fullmatch(form.get(key, "")) for key in ("id", "module_id")):
                raise _Rejected(_INVALID)
            source = {key: form.get(key, "") for key in ("id", "module_id", "repository")}
            source["repository"] = repositories.normalize_repository(source["repository"])
            if source["repository"].split("/")[3] not in cast(list[str], policy["owners"]):
                raise _Rejected(_INVALID)
            if form.get("distribution"):
                source["distribution"] = form["distribution"]
                if canonicalize_name(source["distribution"]) != canonicalize_name("luigi-web-" + source["module_id"]):
                    raise _Rejected(_INVALID)
            repositories.register_repository(source, trust_confirmed=True)
            notice = "Repository registered. No code was installed."
        else:
            source_id, sha256 = form.get("source_id", ""), form.get("sha256", "")
            if not _IDENTIFIER.fullmatch(source_id) or not _SHA256.fullmatch(sha256):
                raise _Rejected(_INVALID)
            source = repositories.approved_source(source_id)
            if action == "install":
                tag, asset = form.get("tag", ""), form.get("asset", "")
                if not _TAG.fullmatch(tag) or ".." in tag or len(asset) > 240 or not _ASSET.fullmatch(asset):
                    raise _Rejected(_INVALID)
                try:
                    name, _, _, tags = parse_wheel_filename(asset)
                    if str(name) != source["distribution"] or {str(item) for item in tags} != {"py3-none-any"}:
                        raise ValueError
                except ValueError:
                    raise _Rejected(_INVALID) from None
                repositories.queue_install(source_id, tag, asset, sha256, trust_confirmed=True)
                notice = "Installation request saved to the queue."
            else:
                if not any(release["sha256"] == sha256 for release in repositories.list_releases(source_id)):
                    raise _Rejected("Rollback requires a previously installed approved release.")
                repositories.queue_rollback(source_id, sha256, trust_confirmed=True)
                notice = "Rollback request saved to the queue."
        return _render(request, notice=notice)
    except _Rejected as rejected:
        return _render(request, error=rejected.message, status_code=rejected.status_code, load=False)
    except repositories.ModuleInstallError:
        return _render(request, error=_INVALID, status_code=422, load=False)
    except Exception:
        return _render(request, error=_UNAVAILABLE, status_code=503, load=False)


async def _submit(request: Request, action: str, fields: set[str]) -> Response:
    try:
        form = await _form(request, fields)
    except _Rejected as rejected:
        return _render(request, error=rejected.message, status_code=rejected.status_code, load=False)
    except Exception:
        return _render(request, error=_UNAVAILABLE, status_code=503, load=False)
    return await run_in_threadpool(_save, request, action, form)


@router.post(_BASE + "/sources", response_class=HTMLResponse)
async def register_source(request: Request) -> Response:
    return await _submit(request, "register", {"id", "module_id", "repository", "distribution"})


@router.post(_BASE + "/install", response_class=HTMLResponse)
async def queue_install(request: Request) -> Response:
    return await _submit(request, "install", {"source_id", "tag", "asset", "sha256"})


@router.post(_BASE + "/rollback", response_class=HTMLResponse)
async def queue_rollback(request: Request) -> Response:
    return await _submit(request, "rollback", {"source_id", "sha256"})