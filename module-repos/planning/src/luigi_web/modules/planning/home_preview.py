"""Read-only Home proposal; all interactive state lives in the browser."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ... import clock
from ...auth import require_auth
from ...core import templating as core

router = APIRouter()
templates = core.create_templates()


@router.get("/home/preview", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def home_preview(request: Request):
    today = clock.local_today()
    tomorrow = today + timedelta(days=1)
    later = today + timedelta(days=3)
    registry = getattr(request.app.state, "modules", None)
    shortcuts = [
        {"label": label, "href": href, "icon": icon}
        for module_id, label, href, icon in (
            ("media", "Games", "/games", "gamepad-2"),
            ("cards", "Trading cards", "/cards", "layers"),
        )
        if registry is not None and registry.is_enabled(module_id)
    ]
    seed = {
        "today": today.isoformat(),
        "tasks": [
            {
                "id": "example-task-1", "title": "Example: review the launch checklist",
                "notes": "Check the three sample milestones in this fictional project.",
                "due": (today - timedelta(days=1)).isoformat(), "today": True,
                "priority": "High", "done": False, "attentionDismissed": False,
            },
            {
                "id": "example-task-2", "title": "Example: draft a project outline",
                "notes": "Sketch a short introduction and the next two sample steps.",
                "due": today.isoformat(), "today": True,
                "priority": "High", "done": False, "attentionDismissed": False,
            },
            {
                "id": "example-task-3", "title": "Example: sort reference notes",
                "notes": "Group the fictional notes by topic. No deadline is required.",
                "due": "", "today": True,
                "priority": "Normal", "done": False, "attentionDismissed": False,
            },
            {
                "id": "example-task-4", "title": "Example: prepare a practice session",
                "notes": "Choose an exercise for the sample session.",
                "due": tomorrow.isoformat(), "today": False,
                "priority": "Normal", "done": False, "attentionDismissed": False,
            },
            {
                "id": "example-task-5", "title": "Example: review the weekly plan",
                "notes": "Review this fictional week's plan and choose one next step.",
                "due": later.isoformat(), "today": False,
                "priority": "Normal", "done": False, "attentionDismissed": False,
            },
        ],
        "habits": [
            {"id": "example-habit-1", "title": "Example: stretch break", "done": False},
            {"id": "example-habit-2", "title": "Example: read a page", "done": False},
        ],
        "slots": [
            {"date": tomorrow.isoformat(), "time": "09:30", "label": "Example focus block"},
            {"date": later.isoformat(), "time": "15:00", "label": "Example planning block"},
        ],
    }
    return templates.TemplateResponse(
        request=request,
        name="home_preview.html",
        context={
            "active_nav": "home", "page_title": "Home preview",
            "preview_seed": seed, "preview_date": today.strftime("%A, %B %d"),
            "preview_shortcuts": shortcuts,
        },
        headers={"Cache-Control": "no-store"},
    )