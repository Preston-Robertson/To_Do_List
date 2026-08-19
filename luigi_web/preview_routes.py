"""Admin UI for the constrained Preview deployment helper."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import preview
from .auth import (
    DEPLOY_COOKIE_NAME,
    deploy_is_configured,
    deploy_lock_response,
    deploy_unlock_response,
    is_deploy_authenticated,
    require_auth,
    require_deploy_auth,
)
from .paths import TEMPLATES_DIR

router = APIRouter(prefix="/admin/preview", dependencies=[Depends(require_auth)])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _view_state(request: Request, *, result: str = "", error: str = "") -> dict:
    try:
        state = preview.status()
    except Exception as exc:  # noqa: BLE001 - safe helper error
        state = {"configured": preview.helper_available(), "ok": False,
                 "error": str(exc), "exists": False}
    try:
        branches = preview.branches() if preview.helper_available() else []
    except Exception:
        branches = []
    return {
        "request": request,
        "active_nav": "preview",
        "page_title": "Preview deployment",
        "preview": state,
        "branches": branches,
        "deploy_configured": deploy_is_configured(),
        "deploy_unlocked": is_deploy_authenticated(
            request.cookies.get(DEPLOY_COOKIE_NAME)
        ),
        "result": result,
        "error": error,
    }


@router.get("", response_class=HTMLResponse)
def preview_page(request: Request):
    return templates.TemplateResponse("preview.html", _view_state(request))


@router.post("/unlock")
def preview_unlock(request: Request, token: str = Form(...)):
    try:
        response = deploy_unlock_response(token)
        if request.headers.get("HX-Request") == "true":
            response.status_code = 204
            del response.headers["location"]
            response.headers["HX-Redirect"] = "/admin/preview"
        return response
    except (HTTPException, RuntimeError):
        if request.headers.get("HX-Request") == "true":
            return templates.TemplateResponse(
                "preview.html",
                _view_state(request, error="Deployment token is incorrect"),
                status_code=200,
            )
        return RedirectResponse("/admin/preview?unlock=failed", status_code=303)


@router.post("/lock")
def preview_lock(request: Request):
    response = deploy_lock_response()
    if request.headers.get("HX-Request") == "true":
        response.status_code = 204
        del response.headers["location"]
        response.headers["HX-Redirect"] = "/admin/preview"
    return response


@router.post(
    "/{action}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_deploy_auth)],
)
def preview_mutation(request: Request, action: str, branch: str = Form(default="")):
    try:
        result = preview.mutate(action, branch=branch)
        message = (
            f"Preview {action} completed"
            + (f" at {result.get('commit')}" if result.get("commit") else "")
        )
        context = _view_state(request, result=message)
    except (ValueError, RuntimeError) as exc:
        context = _view_state(request, error=str(exc))
    return templates.TemplateResponse("partials/preview_status.html", context)