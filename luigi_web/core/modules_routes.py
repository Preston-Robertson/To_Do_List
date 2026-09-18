"""Authenticated module management without runtime package installation."""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from .module_registry import ModuleConfigurationError, ModuleRegistry
from .module_settings import read_selection, save_selection
from .templating import create_templates

router = APIRouter()
templates = create_templates()


def _render(request: Request, *, error: str = "", saved: bool = False, status_code: int = 200) -> Response:
    registry = request.app.state.modules
    configured = os.environ.get("LUIGI_WEB_MODULES")
    try:
        if configured is None:
            configured = read_selection()
        desired = ModuleRegistry(registry.catalog, configured) if configured is not None else registry
    except ModuleConfigurationError:
        desired = registry
        error = error or "Saved module selection is invalid"
    context: dict[str, Any] = {
        "request": request,
        "page_title": "Modules",
        "active_nav": "modules",
        "catalog": registry.catalog,
        "enabled_ids": {module.id for module in registry.enabled},
        "selected_ids": {module.id for module in desired.enabled},
        "module_status": getattr(request.app.state, "module_status", {}),
        "environment_managed": "LUIGI_WEB_MODULES" in os.environ,
        "pending_restart": {module.id for module in desired.enabled} != {module.id for module in registry.enabled},
        "saved": saved,
        "error": error,
    }
    response = templates.TemplateResponse("modules.html", context, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/modules", response_class=HTMLResponse)
def modules_page(request: Request) -> Response:
    return _render(request)


@router.post("/modules", response_class=HTMLResponse)
async def modules_save(request: Request) -> Response:
    try:
        form = await request.form(max_fields=128, max_files=0)
        selected = [str(value) for value in form.getlist("enabled")]
        save_selection(selected)
    except ModuleConfigurationError as exc:
        return _render(request, error=str(exc), status_code=422)
    return _render(request, saved=True)