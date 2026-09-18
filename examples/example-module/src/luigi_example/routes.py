"""Read-only status route; authentication is applied by the host registry."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from luigi_web.core.templating import create_templates

from .manifest import module

router = APIRouter(prefix="/extensions/example")
templates = create_templates(package="luigi_example", namespace="example")


@router.get("", response_class=HTMLResponse)
def status(request: Request):
    state = request.app.state.module_status.get(module.id, "enabled")
    return templates.TemplateResponse(
        request=request,
        name="example/status.html",
        context={
            "page_title": module.label,
            "active_nav": module.id,
            "module": module,
            "module_status": state,
        },
    )